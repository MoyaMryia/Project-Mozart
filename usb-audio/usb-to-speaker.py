#!/usr/bin/env python3
"""把 USB 桥 bulk IN 的音频转发到本机扬声器。

本脚本直连设备 1d6b:0105 的接口 0，按 proto.py 的帧头解帧，把 PCM 交给 aplay。
用于在没有 snd-aloop 的主机上试听 Jetson 侧管线输出。

前置条件：同一接口只能有一路读取方。启动前停止 mozart-usb-audio 服务。

  sudo systemctl stop mozart-usb-audio
  python3 usb-to-speaker.py --seconds 20
  sudo systemctl start mozart-usb-audio

示例：
  python3 usb-to-speaker.py                              # 播放到 default 设备
  python3 usb-to-speaker.py --device plughw:CARD=Device,DEV=0
  python3 usb-to-speaker.py --seconds 30

积压上限由两处决定：aplay 管道 PIPE_BYTES 字节，ALSA 缓冲 BUFFER_PERIODS 个 FAST 周期。
写入按声卡节奏推进，管道与 ALSA 缓冲占满时写端等待。
"""
import argparse
import fcntl
import math
import os
import pathlib
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import proto

try:
    import usb.core
    import usb.util
except ImportError:
    raise SystemExit('需要 pyusb：pip install pyusb')

SERVICE = 'mozart-usb-audio'
# 单元处于这些状态时占用同一接口。重启窗口同样占用，因此按状态名判定。
BUSY_STATES = ('active', 'activating', 'deactivating', 'reloading', 'refreshing')
PIPE_BYTES = 8192              # aplay 管道容量，48 kHz 立体声 16 位下约 43 ms
BUFFER_PERIODS = 4             # ALSA 缓冲，单位为 FAST 周期，约 11 ms
REPORT_SECONDS = 2.0
READ_CHUNK = 65536
READ_TIMEOUT_MS = 300
FLOOR_RMS = 1e-9
SILENT_DBFS = 20 * math.log10(FLOOR_RMS)


class AudioError(RuntimeError):
    """播放设备故障。"""


def rms_dbfs(payload):
    """交错 S16LE 整帧的 RMS，单位 dBFS。每 16 个样本取 1 个。"""
    picked = memoryview(payload).cast('h')[::16]
    mean = sum(int(sample) * int(sample) for sample in picked) / len(picked)
    return 20 * math.log10(max(math.sqrt(mean) / 32768, FLOOR_RMS))


class Counters:
    """接收统计与周期输出。丢弃原因首次出现时打印一行。"""

    def __init__(self):
        self.frames = 0
        self.gaps = 0
        self.rejected = 0
        self.level = SILENT_DBFS
        self.reasons = {}
        self.last_report = time.monotonic()

    def summary(self):
        return (f'收 {self.frames} 帧 缺口 {self.gaps} 丢弃 {self.rejected} '
                f'电平 {self.level:.1f} dBFS')

    def reject(self, reason):
        self.rejected += 1
        if reason in self.reasons:
            return
        self.reasons[reason] = self.rejected
        print(f'丢弃帧：{reason}', file=sys.stderr, flush=True)

    def report(self):
        now = time.monotonic()
        if now - self.last_report < REPORT_SECONDS:
            return
        self.last_report = now
        print(self.summary(), flush=True)


class Player:
    """aplay 子进程；非阻塞写入，等待可写时检查停止标志。"""

    def __init__(self, device, stopping):
        self.stopping = stopping
        self.frames = 0
        self.log = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(
                ['aplay', '-D', device, '-f', 'S16_LE', '-r', str(proto.RATE),
                 '-c', str(proto.CHANNELS), '-t', 'raw', '-q',
                 '--period-time', str(proto.FAST_PERIOD_US),
                 '--buffer-time', str(proto.FAST_PERIOD_US * BUFFER_PERIODS)],
                stdin=subprocess.PIPE, stderr=self.log)
        except OSError as error:
            self.log.close()
            raise AudioError(f'启动 aplay 失败：{error}') from error
        self.fd = self.process.stdin.fileno()
        os.set_blocking(self.fd, False)
        self.limit_pipe(PIPE_BYTES)

    def limit_pipe(self, size):
        """缩小 stdin 管道容量，把管道积压限制在一帧到数帧之间。"""
        try:
            fcntl.fcntl(self.fd, fcntl.F_SETPIPE_SZ, size)
        except OSError as error:
            print(f'管道容量设置失败，沿用内核默认值: {error}', file=sys.stderr, flush=True)

    def failure(self, error):
        """组装写入失败原因；aplay 已退出时附上它的错误输出。"""
        text = ''
        if self.process.poll() is not None:
            self.log.seek(0)
            text = self.log.read().decode(errors='replace').strip()
        suffix = f'；aplay：{text}' if text else ''
        return AudioError(f'播放写入失败：{error}{suffix}')

    def write(self, payload):
        if self.stopping.is_set():
            return
        self.frames += 1
        view = memoryview(payload)
        while view:
            try:
                view = view[os.write(self.fd, view):]
            except BlockingIOError:
                select.select([], [self.fd], [], 0.2)
            except OSError as error:
                raise self.failure(error) from error

    def close(self):
        """关闭 stdin，让 aplay 播完剩余数据后退出。"""
        try:
            try:
                self.process.stdin.close()
            except OSError as error:
                print(f'关闭 aplay 管道失败：{error}', file=sys.stderr, flush=True)
            if self.process.poll() is not None:
                return
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired as error:
                    raise AudioError(f'aplay 无法结束：{error}') from error
        finally:
            self.log.close()


def check_service():
    """systemd 单元占用同一接口时拒绝启动。"""
    try:
        result = subprocess.run(['systemctl', 'is-active', SERVICE],
                                capture_output=True, text=True)
    except FileNotFoundError:
        print('未找到 systemctl，跳过服务占用检查', file=sys.stderr, flush=True)
        return
    state = result.stdout.strip()
    if state in BUSY_STATES:
        raise SystemExit(f'{SERVICE} 状态 {state}，与本脚本争用同一接口。\n'
                         f'先执行：sudo systemctl stop {SERVICE}')


def open_device():
    """打开设备并返回 (device, bulk IN 端点)。接口 0 承载音频与控制。"""
    device = usb.core.find(idVendor=proto.VID, idProduct=proto.PID)
    if device is None:
        raise SystemExit(f'未找到 USB 设备 {proto.VID:04x}:{proto.PID:04x}；检查 USB-C 线、Jetson 侧服务'
                         '与 usbfs 权限（非 root 运行时需要 udev 规则）。')
    try:
        try:
            configuration = device.get_active_configuration()
        except usb.core.USBError:
            device.set_configuration()
            configuration = device.get_active_configuration()
    except usb.core.USBError as error:
        raise SystemExit(f'读取设备配置失败：{error}') from error
    try:
        interface = configuration[(0, 0)]
    except (KeyError, IndexError):
        raise SystemExit('设备没有接口 0')
    endpoint = usb.util.find_descriptor(
        interface, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
        == usb.util.ENDPOINT_IN)
    if endpoint is None:
        raise SystemExit('接口 0 缺少 bulk IN 端点')
    return device, endpoint


def receive(endpoint, sink, stopping, counters):
    """读 USB 并写入 sink；播放故障或 USB 故障时返回 1。"""
    buffer = b''
    expected = {}
    while not stopping.is_set():
        try:
            buffer += bytes(endpoint.read(READ_CHUNK, timeout=READ_TIMEOUT_MS))
        except usb.core.USBTimeoutError:
            counters.report()
            continue
        except usb.core.USBError as error:
            print(f'读 USB 失败：{error}', file=sys.stderr, flush=True)
            return 1
        while True:
            try:
                frame = proto.decode(buffer)
            except proto.FrameError as error:
                print(f'帧格式错误，丢弃缓冲：{error}', file=sys.stderr, flush=True)
                buffer = b''
                break
            if frame is None:
                break
            version, kind, stream, _flags, sequence, _ts, payload = frame
            buffer = buffer[proto.HEADER_BYTES + len(payload):]
            if version != proto.VERSION:
                counters.reject(f'未知协议版本 {version}')
                continue
            if not proto.is_audio_frame(kind, stream, len(payload)):
                counters.reject(f'非音频帧或帧长非法：流 {stream}，{len(payload)} 字节')
                continue
            previous = expected.get(stream)
            if previous is not None and sequence != previous:
                counters.gaps += 1
            expected[stream] = sequence + 1
            counters.frames += 1
            counters.level = rms_dbfs(payload)
            try:
                sink.write(payload)
            except AudioError as error:
                print(str(error), file=sys.stderr, flush=True)
                return 1
            counters.report()
    return 0


def main():
    parser = argparse.ArgumentParser(description='把 USB 桥收到的音频播放到本机扬声器')
    parser.add_argument('--device', default='default', help='aplay 播放设备，默认 default')
    parser.add_argument('--seconds', type=float, default=None, help='运行时长，默认不限')
    arguments = parser.parse_args()
    if arguments.seconds is not None and (not math.isfinite(arguments.seconds)
                                          or arguments.seconds < 0):
        parser.error('--seconds 必须是有限的非负秒数')

    stopping = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    check_service()
    counters = Counters()
    player = None
    code = 1
    try:
        _device, endpoint = open_device()
        player = Player(arguments.device, stopping)
        print(f'播放到 {arguments.device}；Ctrl-C 结束', flush=True)
        timer = None
        if arguments.seconds is not None:
            timer = threading.Timer(arguments.seconds, stopping.set)
            timer.start()
        try:
            code = receive(endpoint, player, stopping, counters)
        finally:
            if timer is not None:
                timer.cancel()
    except AudioError as error:
        print(f'播放设备故障：{error}', file=sys.stderr, flush=True)
        code = 1
    finally:
        stopping.set()
        if player is not None:
            try:
                player.close()
            except AudioError as error:
                print(f'关闭播放设备失败：{error}', file=sys.stderr, flush=True)
                code = 1
    print(f'结束：{counters.summary()}', flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main())
