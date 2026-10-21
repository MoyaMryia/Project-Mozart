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
