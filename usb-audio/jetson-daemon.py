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
from src import AsyncSrc

MIC = os.environ.get('MOZART_MIC', 'plughw:CARD=Loopback,DEV=1,SUBDEV=1')
SPEAKER = os.environ.get('MOZART_SPEAKER', 'plughw:CARD=Loopback,DEV=0,SUBDEV=0')
# M2 低延迟：默认启用快流（128 样本周期）；置 0 回到 20 ms 兼容模式。
FAST = os.environ.get('MOZART_FAST', '1') != '0'
FAST_PERIOD_US = int(1e6 * proto.FAST_SAMPLES / proto.RATE)   # 2667 µs
MNT = os.environ.get('MOZART_FFS', '/dev/ffs-mozart')
UDC_FILE = '/sys/kernel/config/usb_gadget/mozart/UDC'

DESCS_MAGIC_V2 = 3
STRINGS_MAGIC = 2
HAS_FS = 1
HAS_HS = 2
EVENT_BIND, EVENT_UNBIND, EVENT_ENABLE, EVENT_DISABLE = 0, 1, 2, 3
REPORT_SECONDS = 5.0


def descriptors():
    """接口 0：20 ms 兼容流 + 控制（守护进程自有 ep1/ep2）。
    接口 1：128 样本快流专用端点（ep3/ep4，由 mozart-pre 直接持有，
    见 --usb-endpoint）。端点地址按声明顺序分配：0x03 OUT / 0x84 IN。"""
    head = struct.pack('<III', DESCS_MAGIC_V2, 0, HAS_FS | HAS_HS)
    counts = struct.pack('<II', 6, 6)
    iface0 = struct.pack('<BBBBBBBBB', 9, 4, 0, 0, 2, 0xff, 0, 0, 1)
    out_fs = struct.pack('<BBBBHB', 7, 5, 0x01, 2, 64, 0)
    in_fs = struct.pack('<BBBBHB', 7, 5, 0x82, 2, 64, 0)
    iface1 = struct.pack('<BBBBBBBBB', 9, 4, 1, 0, 2, 0xff, 0, 0, 1)  # 与接口 0 共用字符串索引 1
    fast_out_fs = struct.pack('<BBBBHB', 7, 5, 0x03, 2, 64, 0)
    fast_in_fs = struct.pack('<BBBBHB', 7, 5, 0x84, 2, 64, 0)
    out_hs = struct.pack('<BBBBHB', 7, 5, 0x01, 2, 512, 0)
    in_hs = struct.pack('<BBBBHB', 7, 5, 0x82, 2, 512, 0)
    fast_out_hs = struct.pack('<BBBBHB', 7, 5, 0x03, 2, 512, 0)
    fast_in_hs = struct.pack('<BBBBHB', 7, 5, 0x84, 2, 512, 0)
    fs_block = iface0 + out_fs + in_fs + iface1 + fast_out_fs + fast_in_fs
    hs_block = iface0 + out_hs + in_hs + iface1 + fast_out_hs + fast_in_hs
    body = counts + fs_block + hs_block
    blob = head + body
    return blob[:4] + struct.pack('<I', len(blob)) + blob[8:]


def strings():
    # 本机内核接受 UTF-8 原文写法；带 1 字节长度的变体返回 EINVAL。
    payload = struct.pack('<IIII', STRINGS_MAGIC, 0, 1, 1)
    payload += struct.pack('<H', 0x0409) + b'Mozart USB audio\x00'
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
        self.last_frame_at = 0.0
        self.gaps = 0
        self.expected_seq = {}
        # 跨时钟域适配（host→jetson）：虚拟水位 = 收到帧 - 已写帧
        self.return_src = AsyncSrc(channels=proto.CHANNELS, target_frames=4.0)
        self.return_level = 0
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
                arecord = ['arecord', '-D', MIC, '-f', 'S16_LE', '-r', str(proto.RATE),
                           '-c', str(proto.CHANNELS), '-t', 'raw', '-q']
                if FAST:
                    arecord += ['--period-time', str(FAST_PERIOD_US),
                                '--buffer-time', str(FAST_PERIOD_US * 6)]
                self.mic = self.spawn(arecord,
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
            aplay = ['aplay', '-D', SPEAKER, '-f', 'S16_LE', '-r', str(proto.RATE),
                     '-c', str(proto.CHANNELS), '-t', 'raw', '-q']
            if FAST:
                aplay += ['--period-time', str(FAST_PERIOD_US),
                          '--buffer-time', str(FAST_PERIOD_US * 6)]
            self.speaker = self.spawn(aplay,
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
        self.drain_speaker_backlog(proto.FRAME_BYTES)
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


    # --- 过期帧排空（M2 延迟修复）---
    def pipe_backlog(self, fd):
        """FIONREAD 查询管道积压字节数；失败返回 0。"""
        try:
            import fcntl
            return struct.unpack('i', fcntl.ioctl(fd, 0x541B, b'\x00' * 4))[0]
        except (OSError, ImportError):
            return 0

    def drain_capture_backlog(self, stream, chunk_bytes, keep_frames):
        """积压超过 keep_frames 时丢最旧帧，计数到 dropped。"""
        backlog = self.pipe_backlog(stream.fileno())
        limit = chunk_bytes * keep_frames
        if backlog <= limit:
            return
        stale = backlog - limit
        while stale > 0:
            try:
                got = os.read(stream.fileno(), min(stale, 65536))
            except (OSError, ValueError):
                break
            if not got:
                break
            stale -= len(got)
        self.dropped += 1
        self.last_error = f'采集积压排空 {stale + (backlog - limit) if False else ""}'
        self.last_error = '采集积压排空（丢弃最旧帧）'

    def drain_speaker_backlog(self, keep_bytes):
        """aplay stdin 积压超限时从读端排空旧数据（写端无读权限，借 /proc/self/fd）。"""
        fd = self.speaker_fd
        if fd < 0:
            return
        backlog = self.pipe_backlog(fd)
        if backlog <= keep_bytes:
            return
        try:
            read_handle = os.open(f'/proc/self/fd/{fd}', os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return
        try:
            stale = backlog - keep_bytes
            while stale > 0:
                got = os.read(read_handle, min(stale, 65536))
                if not got:
                    break
                stale -= len(got)
            self.dropped += 1
            self.last_error = '播放积压排空（丢弃最旧帧）'
        finally:
            os.close(read_handle)

    # --- 搬运 ---
    def mic_pump(self, generation):
        sequence = 0
        silent = 0
        keep = 1
        while self.active.is_set() and not self.stopping.is_set() and generation == self.generation:
            if self.mic is None or self.mic.stdout is None:
                time.sleep(0.2)
                continue
            chunk_size = proto.FAST_BYTES if FAST else proto.FRAME_BYTES
            self.drain_capture_backlog(self.mic.stdout, chunk_size, keep)
            payload = self.mic.stdout.read(chunk_size)
            if len(payload) < chunk_size:
                if self.stopping.is_set():
                    return
                self.fail('采集流结束')
                self.restart_capture()
                continue
            samples = memoryview(payload).cast('h')
            picked = samples[::16]
            rms = (sum(int(s) * int(s) for s in picked) / len(picked)) ** 0.5 / 32768
            silence_limit = proto.FAST_SILENCE_FRAMES if FAST else proto.SILENCE_FRAMES
            if rms < proto.SILENCE_RMS:
                silent += 1
                if silent > silence_limit:
                    silent = 0
                    self.restart_capture()
                    continue
            else:
                silent = 0
            self.last_rms = rms
            if not self.active.is_set() or generation != self.generation:
                return
            stream_id = proto.STREAM_FAST if FAST else proto.STREAM_AUDIO
            frame = proto.encode(proto.TYPE_PCM, sequence, int(time.monotonic() * 1e6),
                                 payload, stream=stream_id)
            try:
                os.write(self.ep_in, frame)
            except OSError as error:
                self.fail(f'写 IN 端点失败: {error}')
                self.active.clear()
                return
            sequence += 1
            self.frames_in += 1
            self.last_frame_at = time.monotonic()

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
                if kind != proto.TYPE_PCM:
                    continue
                if stream not in (proto.STREAM_AUDIO, proto.STREAM_FAST):
                    continue
                if version != proto.VERSION:
                    self.fail(f'未知协议版本 {version}')
                    continue
                # 延迟打点：帧 ts_us（pre 打包时刻，jetson 时钟）→ USB 到达
                if os.environ.get('MOZART_TRACE') and stream == proto.STREAM_AUDIO:
                    now_us = time.monotonic() * 1e6
                    print(f'[trace-j] seq={seq} age={(now_us - _ts)/1000:.1f}ms',
                          flush=True)
                expected = self.expected_seq.get(stream)
                if expected is not None and seq != expected:
                    self.gaps += 1
                self.expected_seq[stream] = seq + 1
                self.frames_out += 1
                self.return_level += 1
                ratio = self.return_src.ratio(max(self.return_level, 1))
                adapted = self.return_src.process(payload, ratio)
                self.write_speaker(adapted)
                self.return_level -= len(adapted) // (proto.CHANNELS * proto.SAMPLE_BYTES)

    def silencer(self):
        chunk = b'\x00' * proto.FRAME_BYTES
        while not self.stopping.is_set():
            if self.speaker_fd >= 0 and not self.active.is_set():
                self.write_speaker(chunk)
            time.sleep(0.02)

    def watchdog(self):
        """主机重启后端点可能失效：链路在用但不再出帧时重新打开。"""
        while not self.stopping.is_set():
            time.sleep(2.0)
            if not self.active.is_set() or self.last_frame_at == 0:
                continue
            if time.monotonic() - self.last_frame_at > 3.0:
                self.fail('发送停止，重新打开端点')
                self.disable()
                time.sleep(0.5)
                if not self.stopping.is_set():
                    self.enable()

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
        threading.Thread(target=self.watchdog, daemon=True).start()
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
