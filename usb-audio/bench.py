#!/usr/bin/env python3
"""USB 音频桥测量脚本：往返延迟、序号缺口、双向帧计数。

用法（需要 USB 已连接，Jetson 守护进程在运行）：

    sudo python3 usb-audio/bench.py --frames 3000

脚本发送带序号的 PCM 帧并统计回传帧，运行期间不要关闭守护进程。
"""
import argparse
import statistics
import struct
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import proto

import usb.core
import usb.util

VID, PID = 0x1d6b, 0x0105


def open_device():
    device = usb.core.find(idVendor=VID, idProduct=PID)
    if device is None:
        raise SystemExit('未找到 %04x:%04x' % (VID, PID))
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


def tone(frames, frequency=1000.0):
    samples = int(proto.RATE*frames/proto.FRAME_MS/1000)
    out = bytearray()
    for index in range(samples):
        value = int(12000*__import__('math').sin(2*3.141592653589793*frequency*index/proto.RATE))
        out += struct.pack('<hh', value, value)
    return bytes(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frames', type=int, default=3000, help='发送帧数')
    parser.add_argument('--interval', type=float, default=0.02, help='发送间隔，秒')
    arguments = parser.parse_args()

    _device, out, inp = open_device()
    payload = tone(proto.FRAME_MS)
    latencies = []
    received = 0
    gaps = 0
    expected = 0
    pending = b''
    started = time.monotonic()
    for sequence in range(arguments.frames):
        frame = proto.encode(proto.TYPE_PCM, sequence, int(time.monotonic()*1e6), payload)
        out.write(frame, timeout=2000)
        deadline = time.monotonic() + arguments.interval
        while time.monotonic() < deadline:
            try:
                pending += bytes(inp.read(65536, timeout=50))
            except usb.core.USBTimeoutError:
                break
            while True:
                decoded = proto.decode(pending)
                if decoded is None:
                    break
                _version, kind, stream, _flags, seq, ts_us, body = decoded
                pending = pending[proto.HEADER_BYTES + len(body):]
                if kind != proto.TYPE_PCM or stream != proto.STREAM_AUDIO:
                    continue
                received += 1
                if seq != expected:
                    gaps += 1
                expected = seq + 1
                latencies.append(int(time.monotonic()*1e6) - ts_us)
    elapsed = time.monotonic() - started
    if latencies:
        ordered = sorted(latencies)
        print(f'发送 {arguments.frames} 帧，接收 {received} 帧，序号缺口 {gaps}，'
              f'用时 {elapsed:.2f}s')
        print(f'帧龄中位 {ordered[len(ordered)//2]/1000:.2f} ms，'
              f'P95 {ordered[int(len(ordered)*0.95)]/1000:.2f} ms，'
              f'最大 {ordered[-1]/1000:.2f} ms')
    else:
        print(f'发送 {arguments.frames} 帧，未收到回传')


if __name__ == '__main__':
    main()
