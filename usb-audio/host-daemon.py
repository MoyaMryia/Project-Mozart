#!/usr/bin/env python3
"""主机侧守护进程：USB bulk ↔ ALSA loopback（虚拟声卡）。

设备约定（一张 snd-aloop，两个 substream）：
  substream 0：本进程播放收到的音频 → 应用从 plughw:1,1,0 录音（虚拟麦克风）
  substream 1：应用播放到 plughw:1,0,1      → 本进程采集（虚拟扬声器）

USB 未连接时进入离线模式：虚拟声卡照常工作（麦克风喂静音、扬声器数据丢弃），
便于没有本地 USB 连接时调试上层应用。
"""
import os
import signal
import subprocess
import sys
import threading
import time

import usb.core
import usb.util

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import proto

VID, PID = 0x1d6b, 0x0105
MIC_SINK = os.environ.get('MOZART_MIC_SINK', 'plughw:1,0,0')
APP_SOURCE = os.environ.get('MOZART_APP_SOURCE', 'plughw:1,1,1')


class Bridge:
    def __init__(self):
        self.stopping = threading.Event()
        self.device = None
        self.ep_out = self.ep_in = None
        self.speaker = None
        self.speaker_fd = -1
        self.capture = None
        self.audio_lock = threading.Lock()
        self.link_lock = threading.Lock()
        self.pending = b''
        self.sent = 0
        self.received = 0
        self.dropped = 0

    # --- ALSA ---
    def spawn(self, argv, **kwargs):
        return subprocess.Popen(argv, preexec_fn=os.setsid, **kwargs)

    def start_audio(self):
        self.speaker = self.spawn(
            ['aplay', '-D', MIC_SINK, '-f', 'S16_LE', '-r', str(proto.RATE),
             '-c', str(proto.CHANNELS), '-t', 'raw', '-q'],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.speaker_fd = self.speaker.stdin.fileno()
        os.set_blocking(self.speaker_fd, False)
        self.restart_capture()

    def restart_capture(self):
        with self.audio_lock:
            if self.capture and self.capture.poll() is None:
                try:
                    os.killpg(self.capture.pid, signal.SIGTERM)
                except OSError:
                    self.capture.terminate()
                try:
                    self.capture.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.capture.kill()
            for attempt in range(10):
                self.capture = self.spawn(
                    ['arecord', '-D', APP_SOURCE, '-f', 'S16_LE', '-r', str(proto.RATE),
                     '-c', str(proto.CHANNELS), '-t', 'raw', '-q'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                time.sleep(0.4)
                if self.capture.poll() is None:
                    return True
                error = self.capture.stderr.read().decode(errors='replace').strip()
                print(f'采集打开失败({attempt}): {error}', flush=True)
                time.sleep(0.6)
        return False

    def write_speaker(self, payload):
        try:
            os.write(self.speaker_fd, payload)
            return True
        except BlockingIOError:
            return False
        except OSError:
            return False

    # --- USB ---
    def open_usb(self):
        with self.link_lock:
            device = usb.core.find(idVendor=VID, idProduct=PID)
            if device is None:
                return False
            try:
                configuration = device.get_active_configuration()
            except usb.core.USBError:
                try:
                    device.set_configuration()
                    configuration = device.get_active_configuration()
                except usb.core.USBError:
                    return False
            interface = configuration[(0, 0)]
            self.ep_out = usb.util.find_descriptor(
                interface, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
                == usb.util.ENDPOINT_OUT)
            self.ep_in = usb.util.find_descriptor(
                interface, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
                == usb.util.ENDPOINT_IN)
            self.device = device
            self.pending = b''
            print(f'USB 就绪 out=0x{self.ep_out.bEndpointAddress:02x} '
                  f'in=0x{self.ep_in.bEndpointAddress:02x}', flush=True)
            return True

    def drop_usb(self, reason):
        with self.link_lock:
            if self.device is None:
                return
            print(f'USB 断开: {reason}（离线模式）', flush=True)
            try:
                usb.util.dispose_resources(self.device)
            except (usb.core.USBError, OSError):
                pass
            self.device = None
            self.ep_out = self.ep_in = None
            self.pending = b''

    def supervisor(self):
        while not self.stopping.is_set():
            if self.device is None:
                self.open_usb()
            time.sleep(2.0)

    def usb_to_alsa(self):
        while not self.stopping.is_set():
            if self.ep_in is None:
                time.sleep(0.2)
                continue
            try:
                self.pending += bytes(self.ep_in.read(65536, timeout=300))
            except usb.core.USBTimeoutError:
                continue
            except (usb.core.USBError, OSError) as error:
                self.drop_usb(error)
                continue
            while True:
                frame = proto.decode(self.pending)
                if frame is None:
                    if len(self.pending) > 4 << 20:
                        self.pending = b''
                    break
                _version, kind, stream, _flags, _seq, _ts, payload = frame
                self.pending = self.pending[proto.HEADER_BYTES + len(payload):]
                if kind != proto.TYPE_PCM or stream != proto.STREAM_AUDIO:
                    continue
                self.received += 1
                self.write_speaker(payload)

    def alsa_to_usb(self):
        sequence = 0
        silent = 0
        while not self.stopping.is_set():
            if self.capture is None or self.capture.stdout is None:
                time.sleep(0.2)
                continue
            payload = self.capture.stdout.read(proto.FRAME_BYTES)
            if not payload or len(payload) < proto.FRAME_BYTES:
                print('采集流结束，重开', flush=True)
                if self.stopping.is_set():
                    return
                self.restart_capture()
                continue
            samples = memoryview(payload).cast('h')
            picked = samples[::16]
            rms = (sum(int(s) * int(s) for s in picked) / max(len(picked), 1)) ** 0.5 / 32768
            if rms < 1e-6:
                silent += 1
                if silent > 150:
                    silent = 0
                    self.restart_capture()
                    continue
            else:
                silent = 0
            if self.ep_out is None:
                self.dropped += 1
                continue
            frame = proto.encode(proto.TYPE_PCM, sequence, int(time.monotonic() * 1e6), payload)
            try:
                self.ep_out.write(frame, timeout=2000)
            except (usb.core.USBError, OSError) as error:
                self.drop_usb(error)
                continue
            sequence += 1
            self.sent += 1

    def silencer(self):
        chunk = b'\x00' * proto.FRAME_BYTES
        while not self.stopping.is_set():
            if self.speaker_fd >= 0 and self.ep_in is None:
                self.write_speaker(chunk)
            time.sleep(0.02)

    def stop(self, *_):
        self.stopping.set()

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.start_audio()
        self.open_usb()
        threads = [threading.Thread(target=self.supervisor, daemon=True),
                   threading.Thread(target=self.usb_to_alsa, daemon=True),
                   threading.Thread(target=self.alsa_to_usb, daemon=True),
                   threading.Thread(target=self.silencer, daemon=True)]
        for thread in threads:
            thread.start()
        last = None
        while not self.stopping.is_set():
            time.sleep(1.0)
            counter = (self.sent, self.received, self.dropped)
            if counter != last:
                print(f'发 {self.sent} 收 {self.received} 丢 {self.dropped}', flush=True)
                last = counter
        for process in (self.speaker, self.capture):
            if process and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    process.terminate()
        print(f'发 {self.sent} 收 {self.received} 丢 {self.dropped}', flush=True)
        return 0


if __name__ == '__main__':
    sys.exit(Bridge().run())
