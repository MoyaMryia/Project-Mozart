#!/usr/bin/env python3
"""Mozart USB bulk 音频/控制协议（v1）。两侧共用同一份定义。

设计目标：
  1. 音频与控制在同一条 bulk 通道上复用，用 stream 区分。
  2. 未来扩展不改帧头：接收方忽略未知 type/stream 的帧，并保持连接。
  3. 握手携带版本与能力位，能力不匹配时降级运行，链路保持连接。

帧头（小端，24 字节）：
    magic    u16   0x4D5A
    version  u8    1
    type     u8    0=PCM 1=CONTROL 2=TELEMETRY
    stream   u8    逻辑流：0=主音频，1=控制，2.. 预留
    flags    u8    bit0=start bit1=end bit2=urgent，其余预留
    reserved u16   预留，必须为 0
    seq      u32   每 (type,stream) 独立单调递增
    ts_us    u64   发送时刻，单调时钟，微秒
    length   u32   负载字节数
负载：48 kHz / 2 ch / S16，默认 20 ms = 3840 字节。

控制帧负载：opcode u16 + args，见 CONTROL_*。
"""
import struct

MAGIC = 0x4D5A
VERSION = 1
HEADER = struct.Struct('<HBBBBH IQI')
HEADER_BYTES = HEADER.size          # 24

TYPE_PCM = 0
TYPE_CONTROL = 1
TYPE_TELEMETRY = 2

STREAM_AUDIO = 0
STREAM_CONTROL = 1

FLAG_START = 0x01
FLAG_END = 0x02
FLAG_URGENT = 0x04

RATE = 48000
CHANNELS = 2
SAMPLE_BYTES = 2
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * CHANNELS * SAMPLE_BYTES   # 3840
MAX_PAYLOAD = 1 << 20          # 单帧负载上限，发送与接收共用

# 控制操作码（预留：0x1000 以上给实验用途）
CONTROL_HELLO = 0x0001        # 负载：version u8 + 能力位 u32
CONTROL_HELLO_ACK = 0x0002
CONTROL_START = 0x0003
CONTROL_STOP = 0x0004
CONTROL_SET_GAIN = 0x0010     # 负载：i16 增益，单位 0.1 dB
CONTROL_STATS = 0x0020        # 负载：丢帧 u32 + 欠载 u32
CONTROL_PING = 0x0030
CONTROL_PONG = 0x0031

# 能力位：双方按位与协商
CAP_CONTROL = 1 << 0
CAP_TELEMETRY = 1 << 1
CAP_GAIN = 1 << 2


class FrameError(ValueError):
    pass


def encode(kind, seq, timestamp_us, payload=b'', stream=STREAM_AUDIO, flags=0):
    header = HEADER.pack(MAGIC, VERSION, kind, stream, flags, 0, seq, timestamp_us, len(payload))
    return header + payload


def decode(buffer):
    """返回 (version, kind, stream, flags, seq, ts_us, payload)；数据不完整时返回 None。

    未知的 version/type/stream 由调用方决定忽略；magic、长度或保留位异常抛错。
    """
    if len(buffer) < HEADER_BYTES:
        return None
    magic, version, kind, stream, flags, reserved, seq, ts_us, length = HEADER.unpack_from(buffer, 0)
    if magic != MAGIC:
        raise FrameError(f'magic 0x{magic:04x}')
    if length > MAX_PAYLOAD:
        raise FrameError(f'length {length}')
    if reserved:
        raise FrameError(f'reserved {reserved}')
    total = HEADER_BYTES + length
    if len(buffer) < total:
        return None
    return version, kind, stream, flags, seq, ts_us, buffer[HEADER_BYTES:total]


def control(opcode, args=b'', seq=0, timestamp_us=0):
    return encode(TYPE_CONTROL, seq, timestamp_us, struct.pack('<H', opcode) + args,
                  stream=STREAM_CONTROL)


def parse_control(payload):
    if len(payload) < 2:
        raise FrameError('control payload too short')
    opcode = struct.unpack_from('<H', payload, 0)[0]
    return opcode, payload[2:]
