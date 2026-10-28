"""Windows 桥接程序：接收两种帧长，忽略非音频流，拼接不完整读取。"""
import importlib.util
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

USB_ROOT = Path(__file__).resolve().parents[2] / 'usb-audio'
sys.path.insert(0, str(USB_ROOT))
import proto


def load_bridge():
    spec = importlib.util.spec_from_file_location('windows_bridge',
                                                  USB_ROOT / 'windows-bridge.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = load_bridge()


class FakeEndpoint:
    """按顺序返回预先切好的读结果，用尽后结束接收循环。"""

    def __init__(self, chunks, stopping):
        self.chunks = list(chunks)
        self.stopping = stopping

    def read(self, _size, timeout=0):
        if not self.chunks:
            self.stopping.set()
            return b''
        return self.chunks.pop(0)


class FakeSink:
    def __init__(self):
        self.payloads = []
        self.frames = 0

    def write(self, payload):
        self.payloads.append(payload)
        self.frames += 1


class ReceiveTests(unittest.TestCase):
    def test_accepts_both_frame_sizes(self):
        stopping = threading.Event()
        fast = b'\x01\x02' * proto.FAST_SAMPLES * proto.CHANNELS
        legacy = b'\x03\x04' * (proto.RATE * proto.FRAME_MS // 1000) * proto.CHANNELS
        stream = (
            proto.encode(proto.TYPE_TELEMETRY, 0, 0, b'telemetry')
            + proto.encode(proto.TYPE_PCM, 1, 0, fast, stream=proto.STREAM_FAST)
            + proto.encode(proto.TYPE_PCM, 2, 0, legacy, stream=proto.STREAM_AUDIO)
            + proto.encode(proto.TYPE_PCM, 3, 0, b'control', stream=proto.STREAM_CONTROL)
        )
        sink = FakeSink()
        with mock.patch('sys.stderr'):
            bridge.receive(FakeEndpoint([stream], stopping), sink, stopping, show_stats=False)
        self.assertEqual(sink.payloads, [fast, legacy])

    def test_audio_with_unexpected_length_is_skipped(self):
        stopping = threading.Event()
        sink = FakeSink()
        frame = proto.encode(proto.TYPE_PCM, 1, 0, b'x' * 100, stream=proto.STREAM_AUDIO)
        with mock.patch('sys.stderr'):
            bridge.receive(FakeEndpoint([frame], stopping), sink, stopping, show_stats=False)
        self.assertEqual(sink.payloads, [])

    def test_partial_reads_are_buffered(self):
        stopping = threading.Event()
        payload = b'\x05\x06' * (proto.RATE * proto.FRAME_MS // 1000) * proto.CHANNELS
        frame = proto.encode(proto.TYPE_PCM, 7, 0, payload, stream=proto.STREAM_AUDIO)
        sink = FakeSink()
        bridge.receive(FakeEndpoint([frame[:13], frame[13:]], stopping), sink, stopping,
                       show_stats=False)
        self.assertEqual(sink.payloads, [payload])


if __name__ == '__main__':
    unittest.main()
