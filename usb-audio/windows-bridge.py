#!/usr/bin/env python3
"""Windows 配套程序：直接连接 Mozart USB 音频桥，录放与麦克风回传。

用途：演示机换成 Windows 时，不依赖 snd-aloop 与 WDM 虚拟声卡。
本程序自己做设备读写，用普通声卡播放与采集。

Windows 准备：
  1. pip install pyusb sounddevice numpy
  2. 用 Zadig 给 `1d6b:0105` 的接口 0 安装 WinUSB 驱动（一次即可）。

Linux 用法相同，可直接用于测试。

示例：
  python windows-bridge.py --record rx.wav --seconds 30
  python windows-bridge.py --play --seconds 30
  python windows-bridge.py --capture            # 把本机麦克风送到 Jetson
"""
import argparse
import sys
import threading
import time
import wave
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import proto

try:
    import usb.core
    import usb.util
except ImportError:
    raise SystemExit('需要 pyusb：pip install pyusb')

VID, PID = 0x1d6b, 0x0105


def open_device():
    device = usb.core.find(idVendor=VID, idProduct=PID)
    if device is None:
        raise SystemExit('未找到 USB 设备 1d6b:0105；检查线缆与 WinUSB 驱动')
    try:
        configuration = device.get_active_configuration()
    except usb.core.USBError:
        device.set_configuration()
        configuration = device.get_active_configuration()
    interface = configuration[(0, 0)]
    out = usb.util.find_descriptor(
        interface, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
        == usb.util.ENDPOINT_OUT)
    inp = usb.util.find_descriptor(
        interface, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
        == usb.util.ENDPOINT_IN)
    if out is None or inp is None:
        raise SystemExit('设备缺少 bulk 端点')
    return device, out, inp


class Sink:
    """接收端：写 WAV 或用 sounddevice 播放。"""

    def __init__(self, path=None, play=False):
        self.writer = None
        self.stream = None
        self.frames = 0
        if path:
            self.writer = wave.open(str(path), 'wb')
            self.writer.setnchannels(proto.CHANNELS)
            self.writer.setsampwidth(proto.SAMPLE_BYTES)
            self.writer.setframerate(proto.RATE)
        if play:
            import sounddevice as sd
            self.stream = sd.RawOutputStream(samplerate=proto.RATE, channels=proto.CHANNELS,
                                             dtype='int16', blocksize=0)
            self.stream.start()

    def write(self, payload):
        self.frames += 1
        if self.writer:
            self.writer.writeframes(payload)
        if self.stream:
            self.stream.write(payload)

    def close(self):
        if self.writer:
            self.writer.close()
        if self.stream:
            self.stream.stop()
            self.stream.close()


def receive(inp, sink, stopping, show_stats=True):
    buffer = b''
    expected = None
    gaps = 0
    while not stopping.is_set():
        try:
            buffer += bytes(inp.read(65536, timeout=300))
        except usb.core.USBTimeoutError:
            continue
        except usb.core.USBError as error:
            print(f'读 USB 失败：{error}', file=sys.stderr)
            return
        while True:
            try:
                frame = proto.decode(buffer)
            except proto.FrameError as error:
                print(f'帧格式错误，丢弃缓冲：{error}', file=sys.stderr)
                buffer = b''
                break
            if frame is None:
                break
            version, kind, stream, _flags, sequence, _ts, payload = frame
            buffer = buffer[proto.HEADER_BYTES + len(payload):]
            if version != proto.VERSION:
                print(f'未知协议版本 {version}', file=sys.stderr)
                continue
            if kind != proto.TYPE_PCM or stream != proto.STREAM_AUDIO:
                continue
            if expected is not None and sequence != expected:
                gaps += 1
            expected = sequence + 1
            sink.write(payload)
            if show_stats and sink.frames % 50 == 0:
                print(f'已接收 {sink.frames} 帧，缺口 {gaps}', flush=True)


def capture(out, stopping):
    """把本机麦克风送给 Jetson，20 毫秒一帧。"""
    import sounddevice as sd
    import numpy as np
    blocks = int(proto.RATE * proto.FRAME_MS / 1000)
    sequence = 0

    def callback(indata, _frames, _time, status):
        nonlocal sequence
        if status:
            print(f'采集状态：{status}', file=sys.stderr)
        payload = np.asarray(indata[:blocks], dtype=np.int16).tobytes()
        if len(payload) != proto.FRAME_BYTES:
            return
        frame = proto.encode(proto.TYPE_PCM, sequence, int(time.monotonic()*1e6), payload)
        try:
            out.write(frame, timeout=2000)
            sequence += 1
        except usb.core.USBError as error:
            print(f'写 USB 失败：{error}', file=sys.stderr)
            stopping.set()

    with sd.RawInputStream(samplerate=proto.RATE, channels=proto.CHANNELS, dtype='int16',
                           blocksize=blocks, callback=callback):
        while not stopping.is_set():
            time.sleep(0.2)
    print(f'已发送 {sequence} 帧')
    return sequence


def main():
    parser = argparse.ArgumentParser(description='Mozart USB 音频桥 Windows 客户端')
    parser.add_argument('--record', help='把收到的音频写入 WAV')
    parser.add_argument('--play', action='store_true', help='用系统扬声器播放收到的音频')
    parser.add_argument('--capture', action='store_true', help='把系统麦克风发送给 Jetson')
    parser.add_argument('--seconds', type=float, default=None, help='运行时长，默认不限')
    arguments = parser.parse_args()
    if not (arguments.record or arguments.play or arguments.capture):
        parser.error('至少选择一个动作：--record / --play / --capture')

    _device, out, inp = open_device()
    stopping = threading.Event()
    threads = []
    sink = None
    if arguments.record or arguments.play:
        sink = Sink(arguments.record, arguments.play)
        thread = threading.Thread(target=receive, args=(inp, sink, stopping), daemon=True)
        thread.start()
        threads.append(thread)
    if arguments.capture:
        thread = threading.Thread(target=capture, args=(out, stopping), daemon=True)
        thread.start()
        threads.append(thread)

    try:
        if arguments.seconds:
            time.sleep(arguments.seconds)
            stopping.set()
        else:
            while any(thread.is_alive() for thread in threads):
                time.sleep(0.2)
    except KeyboardInterrupt:
        stopping.set()
    finally:
        if sink:
            sink.close()
    print('结束', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
