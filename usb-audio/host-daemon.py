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
import struct
import subprocess
import sys
import threading
import time

import usb.core
import usb.util

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import proto
from pcm import read_block
from src import AsyncSrc, TARGET_BLOCKS

VID, PID = 0x1d6b, 0x0105
MIC_SINK = os.environ.get('MOZART_MIC_SINK', 'plughw:CARD=Loopback,DEV=0,SUBDEV=0')
APP_SOURCE = os.environ.get('MOZART_APP_SOURCE', 'plughw:CARD=Loopback,DEV=1,SUBDEV=1')
# 直连模式：MOZART_DIRECT=1 时使用物理设备，并优先使用接口 1 的 128 样本帧端点。
DIRECT = os.environ.get('MOZART_DIRECT', '0') == '1'
DIRECT_MIC = os.environ.get('MOZART_DIRECT_MIC', 'default')
DIRECT_SPEAKER = os.environ.get('MOZART_DIRECT_SPEAKER', 'default')
REPORT_SECONDS = 2.0


class Bridge:
    def __init__(self):
        self.stopping = threading.Event()
        self.device = None
        self.ep_out = self.ep_in = None
        self.fast_ep_out = self.fast_ep_in = None
        self.speaker = None
        self.speaker_fd = -1
        self.capture = None
        self.audio_lock = threading.Lock()
        self.link_lock = threading.Lock()
        self.return_lock = threading.Lock()
        self.link_generation = 0
        self.sent = 0
        self.received = 0
        self.dropped = 0
        self.errors = 0
        self.gaps = 0
        self.expected_seq = {}
        # 水位与目标均使用音频采样帧，每帧包含 CHANNELS 个样本。
        target_frames = proto.FAST_SAMPLES * TARGET_BLOCKS
        self.return_src = AsyncSrc(channels=proto.CHANNELS, target_frames=target_frames)
        self.last_error = ''
        self.backlog_error = False

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
            device = DIRECT_SPEAKER if DIRECT else MIC_SINK
            aplay = ['aplay', '-D', device, '-f', 'S16_LE', '-r', str(proto.RATE),
                     '-c', str(proto.CHANNELS), '-t', 'raw', '-q',
                     '--period-time', str(proto.FAST_PERIOD_US),
                     '--buffer-time', str(proto.FAST_PERIOD_US * 6)]
            self.speaker = self.spawn(aplay,
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.speaker_fd = self.speaker.stdin.fileno()
            os.set_blocking(self.speaker_fd, False)

    def restart_capture(self):
        with self.audio_lock:
            self.stop_process(self.capture)
            device = DIRECT_MIC if DIRECT else APP_SOURCE
            for attempt in range(10):
                arecord = ['arecord', '-D', device, '-f', 'S16_LE', '-r', str(proto.RATE),
                           '-c', str(proto.CHANNELS), '-t', 'raw', '-q',
                           '--period-time', str(proto.FAST_PERIOD_US),
                           '--buffer-time', str(proto.FAST_PERIOD_US * 6)]
                self.capture = self.spawn(arecord,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
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
        self.drain_speaker_backlog(len(payload))
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

    def play_pcm(self, payload):
        with self.return_lock:
            self.return_src.observe(payload)
            fill = self.pipe_backlog(self.speaker_fd) / (proto.CHANNELS * proto.SAMPLE_BYTES)
            ratio = self.return_src.ratio(fill)
            self.write_speaker(self.return_src.process(payload, ratio))


    # --- 过期帧排空 ---
    def pipe_backlog(self, fd):
        """FIONREAD 查询管道积压字节数；失败记录原因并返回 0。"""
        try:
            import fcntl
            backlog = struct.unpack('i', fcntl.ioctl(fd, 0x541B, b'\x00' * 4))[0]
        except (OSError, ImportError) as error:
            if not self.backlog_error:
                self.fail(f'查询管道水位失败，按空管道处理: {error}')
            self.backlog_error = True
            return 0
        self.backlog_error = False
        return backlog

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
            # 接口 1 只在直连模式下声明；默认路径使用接口 0。
            self.fast_ep_out = self.fast_ep_in = None
            if DIRECT:
                try:
                    fast_interface = configuration[(1, 0)]
                    if device.is_kernel_driver_active(1):
                        device.detach_kernel_driver(1)
                    usb.util.claim_interface(device, 1)
                    self.fast_ep_out = usb.util.find_descriptor(
                        fast_interface, custom_match=lambda e:
                            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
                    self.fast_ep_in = usb.util.find_descriptor(
                        fast_interface, custom_match=lambda e:
                            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
                    if self.fast_ep_out is None or self.fast_ep_in is None:
                        self.fail('接口 1 端点不完整，回退接口 0')
                        self.fast_ep_out = self.fast_ep_in = None
                except (KeyError, IndexError, usb.core.USBError, NotImplementedError) as error:
                    self.fail(f'接口 1 声明失败，回退接口 0: {error}')
            self.link_generation += 1
            fast_note = (f' fast=0x{self.fast_ep_out.bEndpointAddress:02x}'
                         f'/0x{self.fast_ep_in.bEndpointAddress:02x}'
                         if self.fast_ep_out is not None else ' fast=回退接口0')
            print(f'USB 就绪 out=0x{self.ep_out.bEndpointAddress:02x} '
                  f'in=0x{self.ep_in.bEndpointAddress:02x}{fast_note}', flush=True)
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
            self.fast_ep_out = self.fast_ep_in = None
            self.link_generation += 1

    def supervisor(self):
        while not self.stopping.is_set():
            if self.device is None:
                self.open_usb()
            time.sleep(2.0)

    def usb_to_alsa(self):
        buffer = b''
        generation = -1
        while not self.stopping.is_set():
            with self.link_lock:
                endpoint = self.ep_in
                current = self.link_generation
            if endpoint is None:
                time.sleep(0.2)
                continue
            if current != generation:
                generation, buffer = current, b''
            try:
                buffer += bytes(endpoint.read(65536, timeout=300))
            except usb.core.USBTimeoutError:
                continue
            except (usb.core.USBError, OSError) as error:
                self.drop_usb(error)
                continue
            while True:
                try:
                    frame = proto.decode(buffer)
                except proto.FrameError as error:
                    self.fail(f'帧格式错误，丢弃缓冲: {error}')
                    buffer = b''
                    break
                if frame is None:
                    break
                version, kind, stream, _flags, seq, _ts, payload = frame
                buffer = buffer[proto.HEADER_BYTES + len(payload):]
                if not proto.is_audio_frame(kind, stream, len(payload)):
                    continue
                # 延迟打点：帧 ts_us（Jetson 打包时刻）→ 本机收到时刻
                if os.environ.get('MOZART_TRACE'):
                    now_us = int(time.monotonic() * 1e6)
                    print(f'[trace] seq={seq} host-now={now_us} '
                          f'frame_energy={abs(int.from_bytes(payload[:2], "little", signed=True))}',
                          flush=True)
                if version != proto.VERSION:
                    self.fail(f'未知协议版本 {version}')
                    continue
                expected = self.expected_seq.get(stream)
                if expected is not None and seq != expected:
                    self.gaps += 1
                self.expected_seq[stream] = seq + 1
                self.received += 1
                self.play_pcm(payload)

    def alsa_to_usb(self):
        sequence = 0
        silent = 0
        keep = 8
        while not self.stopping.is_set():
            if self.capture is None or self.capture.stdout is None:
                time.sleep(0.2)
                continue
            chunk_size = proto.FAST_BYTES
            self.drain_capture_backlog(self.capture.stdout, chunk_size, keep)
            payload = read_block(self.capture.stdout, chunk_size)
            if len(payload) < chunk_size:
                if self.stopping.is_set():
                    return
                self.fail('采集流结束')
                self.restart_capture()
                continue
            samples = memoryview(payload).cast('h')
            picked = samples[::16]
            rms = (sum(int(s) * int(s) for s in picked) / len(picked)) ** 0.5 / 32768
            block_frames = chunk_size // (proto.CHANNELS * proto.SAMPLE_BYTES)
            silence_limit = proto.silence_blocks(block_frames)
            if rms < proto.SILENCE_RMS:
                silent += 1
                if silent > silence_limit:
                    silent = 0
                    self.restart_capture()
                    continue
            else:
                silent = 0
            with self.link_lock:
                endpoint = self.ep_out
                fast_endpoint = self.fast_ep_out
            if endpoint is None:
                self.dropped += 1
                continue
            # 采集块固定 FAST_BYTES，标签与实际帧长一致。
            stream_id = proto.STREAM_FAST
            # 快流且有专用端点时走接口 1；否则与主流量复用接口 0。
            if DIRECT and fast_endpoint is not None:
                endpoint = fast_endpoint
            frame = proto.encode(proto.TYPE_PCM, sequence, int(time.monotonic() * 1e6),
                                 payload, stream=stream_id)
            if os.environ.get('MOZART_TRACE'):
                samples = memoryview(payload).cast('h')
                energy = max(abs(int(s)) for s in samples[:32])
                if energy > 8000:
                    print(f'[trace-capture] seq={sequence} '
                          f'ts={int(time.monotonic() * 1e6)} energy={energy}', flush=True)
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

    def fast_usb_to_alsa(self):
        """接口 1 专用 IN 端点的接收泵（直连模式且端点存在时）。
        本仓库默认路径不启动该泵；需要 Jetson 侧同时读写接口 1 的进程。"""
        buffer = b''
        generation = -1
        while not self.stopping.is_set():
            with self.link_lock:
                endpoint = self.fast_ep_in
                current = self.link_generation
            if endpoint is None:
                time.sleep(0.2)
                continue
            if current != generation:
                generation, buffer = current, b''
            try:
                buffer += bytes(endpoint.read(65536, timeout=300))
            except usb.core.USBTimeoutError:
                continue
            except (usb.core.USBError, OSError) as error:
                self.drop_usb(error)
                continue
            while True:
                try:
                    frame = proto.decode(buffer)
                except proto.FrameError as error:
                    self.fail(f'帧格式错误，丢弃缓冲: {error}')
                    buffer = b''
                    break
                if frame is None:
                    break
                version, kind, stream, _flags, seq, _ts, payload = frame
                buffer = buffer[proto.HEADER_BYTES + len(payload):]
                if not proto.is_audio_frame(kind, stream, len(payload)):
                    continue
                if version != proto.VERSION:
                    continue
                self.received += 1
                self.play_pcm(payload)

    def silencer(self):
        chunk = b'\x00' * proto.FRAME_BYTES
        while not self.stopping.is_set():
            with self.return_lock:
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
        print(f'发 {self.sent} 收 {self.received} 丢 {self.dropped} 缺口 {self.gaps} '
              f'错 {self.errors}{suffix}',
              flush=True)

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.start_audio()
        self.open_usb()
        targets = [self.supervisor, self.usb_to_alsa, self.alsa_to_usb, self.silencer, self.watchdog]
        if DIRECT:
            print('直连模式：接口 1 需要 Jetson 侧同时读写该接口的进程；默认路径使用接口 0',
                  flush=True)
            targets.append(self.fast_usb_to_alsa)
        for target in targets:
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
