#!/usr/bin/env python3
"""Mozart USB bulk 音频/控制协议（v1）。两侧共用同一份定义。"""
import struct

from proto_generated import *  # noqa: F401,F403

HEADER = struct.Struct(HEADER_FORMAT)
HELLO = struct.Struct(HELLO_FORMAT)
START = struct.Struct(START_FORMAT)
STOP_ACK = struct.Struct(STOP_ACK_FORMAT)
HEADER_BYTES = HEADER.size


class FrameError(ValueError):
    pass


def encode(kind, seq, timestamp_us, payload=b'', stream=STREAM_AUDIO, flags=0):
    header = HEADER.pack(MAGIC, VERSION, kind, stream, flags, 0, seq, timestamp_us, len(payload))
    return header + payload


def decode(buffer):
    """返回帧字段；数据不完整时返回 None，未知流由调用方按需处理。"""
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


def encode_hello(caps=0, preferred_block=FAST_SAMPLES, version=VERSION):
    """编码 HELLO 候选；该消息本身不切换音频流。"""
    try:
        return HELLO.pack(CONTROL_HELLO, version, caps, preferred_block)
    except struct.error as exc:
        raise FrameError(f'invalid HELLO field: {exc}') from exc


def parse_hello(payload):
    """解析完整 HELLO，返回 (version, caps, preferred_block)。"""
    if len(payload) != HELLO_BYTES:
        raise FrameError(f'HELLO length {len(payload)}')
    opcode, version, caps, preferred_block = HELLO.unpack(payload)
    if opcode != CONTROL_HELLO:
        raise FrameError(f'HELLO opcode {opcode}')
    if version != VERSION:
        raise FrameError(f'HELLO version {version}')
    return version, caps, preferred_block
