#!/usr/bin/env python3
"""主机侧守护进程：USB bulk ↔ ALSA loopback（虚拟声卡）。

设备约定（一张 snd-aloop，两个 substream）：
  substream 0：本进程播放收到的音频 → 应用从 plughw:CARD=Loopback,DEV=1,SUBDEV=0 录音
  substream 1：应用播放到 plughw:CARD=Loopback,DEV=0,SUBDEV=1 → 本进程采集

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
MIC_SINK = os.environ.get('MOZART_MIC_SINK', 'plughw:CARD=Loopback,DEV=0,SUBDEV=0')
APP_SOURCE = os.environ.get('MOZART_APP_SOURCE', 'plughw:CARD=Loopback,DEV=1,SUBDEV=1')
REPORT_SECONDS = 2.0


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
        self.errors = 0
        self.last_error = ''

    # --- 记录 ---
    def fail(self, reason):
        self.errors += 1
        self.last_error = reason
        print(f'错误: {reason}', flush=True)

    # --- 子进程 ---
    def spawn(self, argv, **kwargs):
        return subprocess.Popen(argv, start_new_session=True, **kwargs)

    def stop_process(self, process):
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            return
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.fail('子进程无法结束')

    def start_speaker(self):
        with self.audio_lock:
            self.stop_process(self.speaker)
            self.speaker = self.spawn(
                ['aplay', '-D', MIC_SINK, '-f', 'S16_LE', '-r', str(proto.RATE),
                 '-c', str(proto.CHANNELS), '-t', 'raw', '-q'],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.speaker_fd = self.speaker.stdin.fileno()
            os.set_blocking(self.speaker_fd, False)

    def restart_capture(self):
        with self.audio_lock:
            self.stop_process(self.capture)
            for attempt in range(10):
                self.capture = self.spawn(
                    ['arecord', '-D', APP_SOURCE, '-f', 'S16_LE', '-r', str(proto.RATE),
                     '-c', str(proto.CHANNELS), '-t', 'raw', '-q'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                time.sleep(0.4)
                if self.capture.poll() is None:
                    return True
                error = self.capture.stderr.read().decode(errors='replace').strip()
                self.fail(f'采集打开失败({attempt}): {error}')
                time.sleep(0.6)
        return False

    def start_audio(self):
        self.start_speaker()
        self.restart_capture()

    def stop_audio(self):
        with self.audio_lock:
            self.stop_process(self.speaker)
            self.stop_process(self.capture)
            self.speaker = self.capture = None
            self.speaker_fd = -1

    def write_speaker(self, payload):
        if self.speaker_fd < 0:
            return False
        try:
            written = os.write(self.speaker_fd, payload)
        except BlockingIOError:
            return False
        except OSError as error:
            self.fail(f'写虚拟麦克风失败: {error}')
            return False
        if written != len(payload):
            self.fail(f'写虚拟麦克风短写: {written}/{len(payload)}')
            return False
        return True

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
                except usb.core.USBError as error:
                    self.fail(f'配置设备失败: {error}')
                    return False
            try:
                interface = configuration[(0, 0)]
            except (KeyError, IndexError):
                self.fail('设备没有接口 0，忽略')
                return False
            self.ep_out = usb.util.find_descriptor(
                interface, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
                == usb.util.ENDPOINT_OUT)
            self.ep_in = usb.util.find_descriptor(
                interface, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
                == usb.util.ENDPOINT_IN)
            if self.ep_out is None or self.ep_in is None:
                self.fail('设备缺少 bulk 端点')
                return False
            self.device = device
            self.pending = b''
            print(f'USB 就绪 out=0x{self.ep_out.bEndpointAddress:02x} '
                  f'in=0x{self.ep_in.bEndpointAddress:02x}', flush=True)
            return True

    def drop_usb(self, reason):
        with self.link_lock:
            if self.device is None:
                return
            self.fail(f'USB 断开: {reason}')
            try:
                usb.util.dispose_resources(self.device)
            except (usb.core.USBError, OSError) as error:
                self.fail(f'释放设备失败: {error}')
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
            with self.link_lock:
                endpoint = self.ep_in
            if endpoint is None:
                time.sleep(0.2)
                continue
            try:
                self.pending += bytes(endpoint.read(65536, timeout=300))
            except usb.core.USBTimeoutError:
                continue
            except (usb.core.USBError, OSError) as error:
                self.drop_usb(error)
                continue
            while True:
                try:
                    frame = proto.decode(self.pending)
                except proto.FrameError as error:
                    self.fail(f'帧格式错误，丢弃缓冲: {error}')
                    self.pending = b''
                    break
                if frame is None:
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
            if len(payload) < proto.FRAME_BYTES:
                if self.stopping.is_set():
                    return
                self.fail('采集流结束')
                self.restart_capture()
                continue
            samples = memoryview(payload).cast('h')
            picked = samples[::16]
            rms = (sum(int(s) * int(s) for s in picked) / len(picked)) ** 0.5 / 32768
            if rms < 1e-6:
                silent += 1
                if silent > 150:
                    silent = 0
                    self.restart_capture()
                    continue
            else:
                silent = 0
            with self.link_lock:
                endpoint = self.ep_out
            if endpoint is None:
                self.dropped += 1
                continue
            frame = proto.encode(proto.TYPE_PCM, sequence, int(time.monotonic() * 1e6), payload)
            try:
                endpoint.write(frame, timeout=2000)
            except usb.core.USBTimeoutError:
                self.dropped += 1
                continue
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

    def watchdog(self):
        while not self.stopping.is_set():
            time.sleep(1.0)
            if self.speaker is not None and self.speaker.poll() is not None:
                self.fail('aplay 已退出，重新启动')
                self.start_speaker()
            if self.capture is not None and self.capture.poll() is not None:
                self.fail('arecord 已退出，重新启动')
                self.restart_capture()

    def stop(self, *_):
        self.stopping.set()

    def report(self):
        suffix = f' 最近错误 {self.last_error}' if self.last_error else ''
        print(f'发 {self.sent} 收 {self.received} 丢 {self.dropped} 错 {self.errors}{suffix}',
              flush=True)

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.start_audio()
        self.open_usb()
        for target in (self.supervisor, self.usb_to_alsa, self.alsa_to_usb, self.silencer, self.watchdog):
            threading.Thread(target=target, daemon=True).start()
        last_report = time.monotonic()
        while not self.stopping.is_set():
            time.sleep(0.5)
            if time.monotonic() - last_report >= REPORT_SECONDS:
                self.report()
                last_report = time.monotonic()
        self.stop_audio()
        self.report()
        return 0


if __name__ == '__main__':
    sys.exit(Bridge().run())
