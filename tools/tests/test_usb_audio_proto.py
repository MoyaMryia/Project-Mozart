"""USB bulk 协议的自测：往返、边界与错误分支。"""
import pathlib
import struct
import subprocess
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


class GeneratedProtocolTests(unittest.TestCase):
    def test_generated_files_match_schema(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        result = subprocess.run([sys.executable, str(root / 'tools/gen_usb_protocol.py'), '--check'],
                                cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_header_and_constants_are_unchanged(self):
        self.assertEqual(proto.HEADER_BYTES, 24)
        self.assertEqual(proto.HEADER_FORMAT, '<HBBBBHIQI')
        self.assertEqual((proto.FAST_SAMPLES, proto.FAST_BYTES, proto.STREAM_FAST), (128, 512, 2))
        self.assertEqual((proto.VERSION, proto.MAGIC, proto.RATE, proto.CHANNELS,
                          proto.SAMPLE_BYTES), (1, 0x4D5A, 48000, 2, 2))
        self.assertEqual(proto.encode(0, 1, 2, b'x'),
                         bytes.fromhex('5a4d0100000000000100000002000000000000000100000078'))

    def test_fast64_stream_frame(self):
        payload = bytes(range(256))
        frame = proto.encode(proto.TYPE_PCM, 9, 10, payload, stream=proto.STREAM_FAST64)
        decoded = proto.decode(frame)
        self.assertEqual((decoded[2], len(decoded[-1])), (3, proto.FAST64_BYTES))
        self.assertEqual(proto.FAST64_SAMPLES, 64)

    def test_hello_and_start_golden_payloads(self):
        hello = proto.encode_hello(caps=proto.CAP_FAST64, preferred_block=64)
        self.assertEqual(hello, bytes.fromhex('010001080000004000'))
        self.assertEqual(proto.parse_hello(hello), (1, proto.CAP_FAST64, 64))
        start = proto.START.pack(proto.CONTROL_START, proto.FAST64_SAMPLES)
        start_ack = proto.START.pack(proto.CONTROL_START_ACK, proto.FAST64_SAMPLES)
        self.assertEqual(start, bytes.fromhex('03004000'))
        self.assertEqual(start_ack, bytes.fromhex('05004000'))
        stop_ack = proto.STOP_ACK.pack(proto.CONTROL_STOP_ACK, 0x11223344, 0x55667788)
        self.assertEqual(stop_ack, bytes.fromhex('06004433221188776655'))

    def test_hello_rejects_bad_size_opcode_version_and_fields(self):
        with self.assertRaises(proto.FrameError):
            proto.parse_hello(b'')
        malformed = bytearray(proto.encode_hello())
        malformed[0] = 9
        with self.assertRaises(proto.FrameError):
            proto.parse_hello(malformed)
        malformed = bytearray(proto.encode_hello())
        malformed[2] = 9
        with self.assertRaises(proto.FrameError):
            proto.parse_hello(malformed)
        with self.assertRaises(proto.FrameError):
            proto.encode_hello(caps=1 << 40)


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
