#!/usr/bin/env python3
"""Jetson 侧守护进程：FunctionFS bulk ↔ ALSA loopback。

设计要点：
- ALSA 一直打开。USB 未连接时进入离线模式：继续消费管线播放、给管线采集喂静音。
- 主机重新配置（DISABLE 后 ENABLE）时重开端点，只有 UNBIND/SIGTERM 退出进程。
- MOZART_UDC 非空时，写完描述符后绑定 UDC，退出时解绑。
- 端点读写失败时记录原因并结束当前搬运线程；下一次 ENABLE 会重新拉起。
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
REPORT_SECONDS = 5.0


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


def ffs_string(text, lang=0x0409):
    """单个字符串：1 字节字符数，随后 UTF-16LE 编码。"""
    encoded = text.encode('utf-16-le')
    return struct.pack('<B', len(encoded)//2) + encoded


def strings():
    # 语言 ID 与字符串都按内核 functionfs 的格式编码。
    payload = struct.pack('<IIII', STRINGS_MAGIC, 0, 1, 1)
    payload += struct.pack('<H', 0x0409) + ffs_string('Mozart USB audio')
    return payload[:4] + struct.pack('<I', len(payload)) + payload[8:]


class Daemon:
    def __init__(self):
        self.stopping = threading.Event()
        self.active = threading.Event()
        self.audio_lock = threading.Lock()
        self.generation = 0
        self.ep0 = self.ep_out = self.ep_in = -1
        self.mic = self.speaker = None
        self.speaker_fd = -1
        self.frames_in = 0
        self.frames_out = 0
        self.dropped = 0
        self.errors = 0
        self.gaps = 0
        self.expected_seq = None
        self.last_rms = 0.0
        self.last_error = ''
        self.started = 0.0
        self.pending = b''

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

    def restart_capture(self):
        with self.audio_lock:
            self.stop_process(self.mic)
            for attempt in range(10):
                self.mic = self.spawn(
                    ['arecord', '-D', MIC, '-f', 'S16_LE', '-r', str(proto.RATE),
                     '-c', str(proto.CHANNELS), '-t', 'raw', '-q'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                time.sleep(0.4)
                if self.mic.poll() is None:
                    return True
                error = self.mic.stderr.read().decode(errors='replace').strip()
                self.fail(f'采集打开失败({attempt}): {error}')
                time.sleep(0.6)
        return False

    def start_speaker(self):
        with self.audio_lock:
            self.stop_process(self.speaker)
            self.speaker = self.spawn(
                ['aplay', '-D', SPEAKER, '-f', 'S16_LE', '-r', str(proto.RATE),
                 '-c', str(proto.CHANNELS), '-t', 'raw', '-q'],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.speaker_fd = self.speaker.stdin.fileno()
            os.set_blocking(self.speaker_fd, False)

    def start_audio(self):
        self.restart_capture()
        self.start_speaker()

    def stop_audio(self):
        with self.audio_lock:
            self.stop_process(self.mic)
            self.stop_process(self.speaker)
            self.mic = self.speaker = None
            self.speaker_fd = -1

    # --- 错误记录 ---
    def fail(self, reason):
        self.errors += 1
        self.last_error = reason
        print(f'错误: {reason}', flush=True)

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
            written = os.write(self.speaker_fd, payload)
        except BlockingIOError:
            return False
        except OSError as error:
            self.fail(f'写扬声器失败: {error}')
            return False
        if written != len(payload):
            self.fail(f'写扬声器短写: {written}/{len(payload)}')
            return False
        return True

    # --- 搬运 ---
    def mic_pump(self, generation):
        sequence = 0
        silent = 0
        while self.active.is_set() and not self.stopping.is_set() and generation == self.generation:
            if self.mic is None or self.mic.stdout is None:
                time.sleep(0.2)
                continue
            payload = self.mic.stdout.read(proto.FRAME_BYTES)
            if len(payload) < proto.FRAME_BYTES:
                if self.stopping.is_set():
                    return
                self.fail('采集流结束')
                self.restart_capture()
                continue
            samples = memoryview(payload).cast('h')
            picked = samples[::16]
            rms = (sum(int(s) * int(s) for s in picked) / len(picked)) ** 0.5 / 32768
            if rms < proto.SILENCE_RMS:
                silent += 1
                if silent > proto.SILENCE_FRAMES:
                    silent = 0
                    self.restart_capture()
                    continue
            else:
                silent = 0
            self.last_rms = rms
            if not self.active.is_set() or generation != self.generation:
                return
            frame = proto.encode(proto.TYPE_PCM, sequence, int(time.monotonic() * 1e6), payload)
            try:
                os.write(self.ep_in, frame)
            except OSError as error:
                self.fail(f'写 IN 端点失败: {error}')
                self.active.clear()
                return
            sequence += 1
            self.frames_in += 1

    def usb_pump(self, generation):
        while self.active.is_set() and not self.stopping.is_set() and generation == self.generation:
            if self.ep_out < 0:
                return
            ready, _, _ = select.select([self.ep_out], [], [], 0.3)
            if not ready:
                continue
            try:
                chunk = os.read(self.ep_out, 65536)
            except OSError as error:
                self.fail(f'读 OUT 端点失败: {error}')
                self.active.clear()
                return
            if not chunk:
                continue
            self.pending += chunk
            while True:
                try:
                    frame = proto.decode(self.pending)
                except proto.FrameError as error:
                    self.fail(f'帧格式错误，丢弃缓冲: {error}')
                    self.pending = b''
                    break
                if frame is None:
                    break
                version, kind, stream, _flags, seq, _ts, payload = frame
                self.pending = self.pending[proto.HEADER_BYTES + len(payload):]
                if kind != proto.TYPE_PCM or stream != proto.STREAM_AUDIO:
                    continue
                if version != proto.VERSION:
                    self.fail(f'未知协议版本 {version}')
                    continue
                if self.expected_seq is not None and seq != self.expected_seq:
                    self.gaps += 1
                self.expected_seq = seq + 1
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
        suffix = f' 最近错误 {self.last_error}' if self.last_error else ''
        print(f'发 {self.frames_in} 收 {self.frames_out} 丢 {self.dropped} 缺口 {self.gaps} '
              f'错 {self.errors} 采集RMS {level:.1f} dBFS{suffix}', flush=True)

    def enable(self):
        generation = self.generation + 1
        self.generation = generation
        try:
            ep_out = os.open(f'{MNT}/ep1', os.O_RDWR)
            ep_in = os.open(f'{MNT}/ep2', os.O_RDWR)
        except OSError as error:
            self.fail(f'打开端点失败: {error}')
            for fd in (locals().get('ep_out', -1), locals().get('ep_in', -1)):
                if fd >= 0:
                    os.close(fd)
            return
        self.ep_out, self.ep_in = ep_out, ep_in
        self.pending = b''
        self.active.set()
        threading.Thread(target=self.usb_pump, args=(generation,), daemon=True).start()
        threading.Thread(target=self.mic_pump, args=(generation,), daemon=True).start()
        print('enabled', flush=True)

    def disable(self):
        self.active.clear()
        self.generation += 1
        time.sleep(0.2)
        self.close_endpoints()
        print('disabled；等待重新启用', flush=True)

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: self.stopping.set())
        signal.signal(signal.SIGINT, lambda *_: self.stopping.set())
        self.started = time.monotonic()
        self.ep0 = os.open(f'{MNT}/ep0', os.O_RDWR)
        os.write(self.ep0, descriptors())
        os.write(self.ep0, strings())
        print('descriptors written', flush=True)
        self.start_audio()
        threading.Thread(target=self.silencer, daemon=True).start()
        udc = os.environ.get('MOZART_UDC', '')
        if udc:
            try:
                with open(UDC_FILE, 'w') as handle:
                    handle.write(udc)
                print(f'UDC 绑定 {udc}', flush=True)
            except OSError as error:
                self.fail(f'UDC 绑定失败: {error}（离线模式）')
        last_report = time.monotonic()
        while not self.stopping.is_set():
            ready, _, _ = select.select([self.ep0], [], [], 0.5)
            if ready:
                kind = os.read(self.ep0, 64)[8]
                if kind == EVENT_ENABLE and not self.active.is_set():
                    self.enable()
                elif kind == EVENT_DISABLE and self.active.is_set():
                    self.disable()
                elif kind == EVENT_UNBIND:
                    print('unbind', flush=True)
                    self.stopping.set()
            if time.monotonic() - last_report >= REPORT_SECONDS:
                self.report()
                last_report = time.monotonic()
        self.active.clear()
        self.stop_audio()
        self.close_endpoints()
        if self.ep0 >= 0:
            try:
                os.close(self.ep0)
            except OSError:
                pass
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
