"""USB bulk 协议的自测：往返、边界与错误分支。"""
import pathlib
import struct
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'usb-audio'))
import proto


class ProtocolTests(unittest.TestCase):
    def test_pcm_round_trip(self):
        payload = bytes(range(256)) * 15
        frame = proto.encode(proto.TYPE_PCM, 7, 123456, payload)
        self.assertEqual(len(frame), proto.HEADER_BYTES + len(payload))
        version, kind, stream, flags, seq, ts_us, body = proto.decode(frame)
        self.assertEqual((version, kind, stream, flags), (proto.VERSION, proto.TYPE_PCM,
                                                         proto.STREAM_AUDIO, 0))
        self.assertEqual((seq, ts_us, body), (7, 123456, payload))

    def test_partial_frame_returns_none_until_complete(self):
        frame = proto.encode(proto.TYPE_PCM, 1, 0, b'\x01' * 100)
        self.assertIsNone(proto.decode(frame[:proto.HEADER_BYTES - 1]))
        self.assertIsNone(proto.decode(frame[:-1]))
        self.assertIsNotNone(proto.decode(frame))

    def test_bad_magic_and_reserved_are_rejected(self):
        frame = bytearray(proto.encode(proto.TYPE_PCM, 0, 0, b''))
        frame[0] ^= 0xff
        with self.assertRaises(proto.FrameError):
            proto.decode(bytes(frame))
        frame = bytearray(proto.encode(proto.TYPE_PCM, 0, 0, b''))
        struct.pack_into('<H', frame, 6, 5)          # reserved 必须为 0
        with self.assertRaises(proto.FrameError):
            proto.decode(bytes(frame))

    def test_oversized_payload_is_rejected(self):
        header = proto.HEADER.pack(proto.MAGIC, proto.VERSION, proto.TYPE_PCM, 0, 0, 0, 0, 0,
                                   proto.MAX_PAYLOAD + 1)
        with self.assertRaises(proto.FrameError):
            proto.decode(header)

    def test_control_frame_reports_opcode(self):
        frame = proto.control(proto.CONTROL_PING, b'\x01\x02', seq=3)
        version, kind, stream, _flags, _seq, _ts, payload = proto.decode(frame)
        self.assertEqual((kind, stream), (proto.TYPE_CONTROL, proto.STREAM_CONTROL))
        self.assertEqual(proto.parse_control(payload), (proto.CONTROL_PING, b'\x01\x02'))

    def test_short_control_payload_is_rejected(self):
        with self.assertRaises(proto.FrameError):
            proto.parse_control(b'\x01')


if __name__ == '__main__':
    unittest.main()


class FastStreamTests(unittest.TestCase):
    """M2 快流：帧长、双流独立序号、老对端忽略语义。"""

    def test_fast_constants(self):
        import proto
        self.assertEqual(proto.FAST_SAMPLES, 128)
        self.assertEqual(proto.FAST_BYTES, 512)
        self.assertEqual(proto.STREAM_FAST, 2)
        self.assertEqual(proto.FAST_SILENCE_FRAMES * 1000 * 128 // 48000, 3000)

    def test_fast_roundtrip(self):
        import proto
        payload = bytes(index % 256 for index in range(512))
        frame = proto.encode(proto.TYPE_PCM, 7, 12345, payload, stream=proto.STREAM_FAST)
        version, kind, stream, flags, seq, ts, out = proto.decode(frame)
        self.assertEqual((version, kind, stream, seq, ts), (1, 0, 2, 7, 12345))
        self.assertEqual(out, payload)

    def test_streams_interleave_independently(self):
        import proto
        buffer = proto.encode(0, 1, 1, b'a' * 512, stream=proto.STREAM_FAST)
        buffer += proto.encode(0, 2, 2, b'b' * 3840, stream=proto.STREAM_AUDIO)
        buffer += proto.encode(0, 2, 3, b'c' * 512, stream=proto.STREAM_FAST)
        streams = []
        while buffer:
            version, kind, stream, flags, seq, ts, payload = proto.decode(buffer)
            streams.append((stream, seq, len(payload)))
            buffer = buffer[proto.HEADER_BYTES + len(payload):]
        self.assertEqual(streams, [(2, 1, 512), (0, 2, 3840), (2, 2, 512)])

    def test_unknown_stream_ignored_semantics(self):
        """老接收端按 (type,stream) 白名单跳过未知流，帧边界仍可解析。"""
        import proto
        buffer = proto.encode(0, 1, 1, b'x' * 512, stream=9)
        buffer += proto.encode(0, 1, 1, b'y' * 512, stream=proto.STREAM_FAST)
        parsed = []
        while buffer:
            version, kind, stream, flags, seq, ts, payload = proto.decode(buffer)
            parsed.append(stream)
            buffer = buffer[proto.HEADER_BYTES + len(payload):]
        self.assertEqual(parsed, [9, proto.STREAM_FAST])
