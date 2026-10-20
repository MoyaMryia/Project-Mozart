#!/usr/bin/env python3
"""Jetson 侧守护进程：FunctionFS bulk ↔ ALSA loopback。

设计要点：
- ALSA 一直打开，USB 未连接时进入离线模式：仍然消费管线播放、给管线采集喂静音，
  这样没有本地 USB 连接时管线照常运行，便于单独调试。
- 主机重新配置（DISABLE 后 ENABLE）时重新打开端点，只有 UNBIND/SIGTERM 退出。
- MOZART_UDC 非空时，写完描述符后自动绑定 UDC，退出时解绑。
"""
import math
import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import proto

MIC = os.environ.get('MOZART_MIC', 'plughw:CARD=Loopback,DEV=1,SUBDEV=1')
SPEAKER = os.environ.get('MOZART_SPEAKER', 'plughw:CARD=Loopback,DEV=0,SUBDEV=0')
MNT = os.environ.get('MOZART_FFS', '/dev/ffs-mozart')
UDC_FILE = '/sys/kernel/config/usb_gadget/mozart/UDC'

DESCS_MAGIC_V2 = 3
STRINGS_MAGIC = 2
HAS_FS = 1
HAS_HS = 2
EVENT_BIND, EVENT_UNBIND, EVENT_ENABLE, EVENT_DISABLE = 0, 1, 2, 3


def descriptors():
    head = struct.pack('<III', DESCS_MAGIC_V2, 0, HAS_FS | HAS_HS)
    counts = struct.pack('<II', 3, 3)
    iface = struct.pack('<BBBBBBBBB', 9, 4, 0, 0, 2, 0xff, 0, 0, 1)
    out_fs = struct.pack('<BBBBHB', 7, 5, 0x01, 2, 64, 0)
    in_fs = struct.pack('<BBBBHB', 7, 5, 0x82, 2, 64, 0)
    out_hs = struct.pack('<BBBBHB', 7, 5, 0x01, 2, 512, 0)
    in_hs = struct.pack('<BBBBHB', 7, 5, 0x82, 2, 512, 0)
    body = counts + iface + out_fs + in_fs + iface + out_hs + in_hs
    blob = head + body
    return blob[:4] + struct.pack('<I', len(blob)) + blob[8:]


def strings():
    payload = struct.pack('<IIII', STRINGS_MAGIC, 0, 1, 1)
    payload += struct.pack('<H', 0x0409) + b'Mozart USB audio\x00'
    return payload[:4] + struct.pack('<I', len(payload)) + payload[8:]


class Daemon:
    def __init__(self):
        self.stopping = threading.Event()
        self.active = threading.Event()
        self.audio_lock = threading.Lock()
        self.ep0 = self.ep_out = self.ep_in = -1
        self.mic = self.speaker = None
        self.speaker_fd = -1
        self.frames_in = 0
        self.frames_out = 0
        self.dropped = 0
        self.last_rms = 0.0
        self.started = 0.0
        self.pending = b''

    def spawn(self, argv, **kwargs):
        return subprocess.Popen(argv, preexec_fn=os.setsid, **kwargs)

    def restart_capture(self):
        with self.audio_lock:
            if self.mic and self.mic.poll() is None:
                try:
                    os.killpg(self.mic.pid, signal.SIGTERM)
                except OSError:
                    pass
                try:
                    self.mic.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.mic.kill()
            for attempt in range(10):
                self.mic = self.spawn(
                    ['arecord', '-D', MIC, '-f', 'S16_LE', '-r', str(proto.RATE),
                     '-c', str(proto.CHANNELS), '-t', 'raw', '-q'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                time.sleep(0.4)
                if self.mic.poll() is None:
                    return True
                error = self.mic.stderr.read().decode(errors='replace').strip()
                print(f'采集打开失败({attempt}): {error}', flush=True)
                time.sleep(0.6)
        return False

    def start_audio(self):
        self.restart_capture()
        self.speaker = self.spawn(
            ['aplay', '-D', SPEAKER, '-f', 'S16_LE', '-r', str(proto.RATE),
             '-c', str(proto.CHANNELS), '-t', 'raw', '-q'],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.speaker_fd = self.speaker.stdin.fileno()
        os.set_blocking(self.speaker_fd, False)

    def stop_audio(self):
        for process in (self.mic, self.speaker):
            if process and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
        self.mic = self.speaker = None
        self.speaker_fd = -1

    def close_endpoints(self):
        for name in ('ep_out', 'ep_in'):
            fd = getattr(self, name)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, -1)

    def write_speaker(self, payload):
        try:
            os.write(self.speaker_fd, payload)
            return True
        except BlockingIOError:
            return False
        except OSError:
            return False

    def mic_pump(self):
        sequence = 0
        silent = 0
        while not self.stopping.is_set():
            if self.mic is None or self.mic.stdout is None:
                time.sleep(0.2)
                continue
            payload = self.mic.stdout.read(proto.FRAME_BYTES)
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
            self.last_rms = rms
            if not self.active.is_set():
                self.dropped += 1
                continue
            frame = proto.encode(proto.TYPE_PCM, sequence, int(time.monotonic() * 1e6), payload)
            try:
                os.write(self.ep_in, frame)
            except OSError:
                return
            sequence += 1
            self.frames_in += 1

    def usb_pump(self):
        while self.active.is_set() and not self.stopping.is_set():
            ready, _, _ = select.select([self.ep_out], [], [], 0.3)
            if not ready:
                continue
            try:
                chunk = os.read(self.ep_out, 65536)
            except OSError:
                return
            if not chunk:
                continue
            self.pending += chunk
            while True:
                frame = proto.decode(self.pending)
                if frame is None:
                    if len(self.pending) > 4 << 20:
                        self.pending = b''
                    break
                _, kind, stream, _flags, _seq, _ts, payload = frame
                self.pending = self.pending[proto.HEADER_BYTES + len(payload):]
                if kind != proto.TYPE_PCM or stream != proto.STREAM_AUDIO:
                    continue
                self.frames_out += 1
                self.write_speaker(payload)

    def silencer(self):
        chunk = b'\x00' * proto.FRAME_BYTES
        while not self.stopping.is_set():
            if self.speaker_fd >= 0 and not self.active.is_set():
                self.write_speaker(chunk)
            time.sleep(0.02)

    def report(self):
        level = 20 * math.log10(max(self.last_rms, 1e-9))
        print(f'发 {self.frames_in} 收 {self.frames_out} 丢 {self.dropped} '
              f'采集RMS {level:.1f} dBFS', flush=True)

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: self.stopping.set())
        signal.signal(signal.SIGINT, lambda *_: self.stopping.set())
        self.started = time.monotonic()
        self.ep0 = os.open(f'{MNT}/ep0', os.O_RDWR)
        os.write(self.ep0, descriptors())
        os.write(self.ep0, strings())
        print('descriptors written', flush=True)
        self.start_audio()
        threading.Thread(target=self.mic_pump, daemon=True).start()
        threading.Thread(target=self.silencer, daemon=True).start()
        udc = os.environ.get('MOZART_UDC', '')
        if udc:
            try:
                with open(UDC_FILE, 'w') as handle:
                    handle.write(udc)
                print(f'UDC 绑定 {udc}', flush=True)
            except OSError as error:
                print(f'UDC 绑定失败: {error}（离线模式）', flush=True)
        last = None
        while not self.stopping.is_set():
            ready, _, _ = select.select([self.ep0], [], [], 0.5)
            counter = (self.frames_in, self.frames_out, self.dropped)
            if counter != last and time.monotonic() - self.started > 2:
                self.report()
                last = counter
            if not ready:
                continue
            event = os.read(self.ep0, 64)
            if len(event) < 9:
                continue
            kind = event[8]
            if kind == EVENT_ENABLE:
                if self.active.is_set():
                    continue
                self.ep_out = os.open(f'{MNT}/ep1', os.O_RDWR)
                self.ep_in = os.open(f'{MNT}/ep2', os.O_RDWR)
                self.active.set()
                threading.Thread(target=self.usb_pump, daemon=True).start()
                print('enabled', flush=True)
            elif kind == EVENT_DISABLE:
                print('disabled；等待重新启用', flush=True)
                self.active.clear()
                time.sleep(0.2)
                self.close_endpoints()
            elif kind == EVENT_UNBIND:
                print('unbind', flush=True)
                self.stopping.set()
        self.active.clear()
        self.stop_audio()
        self.close_endpoints()
        if udc:
            try:
                with open(UDC_FILE, 'w') as handle:
                    handle.write('')
            except OSError:
                pass
        self.report()
        return 0


if __name__ == '__main__':
    sys.exit(Daemon().run())
