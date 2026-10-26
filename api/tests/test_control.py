"""M4 二进制控制协议对拍：Unix 数据报 + ctlmsg 契约。

夹具为 api/tests/control_fixture.cpp（真实 ModeController）。
验证：编解码往返、PING/STATUS/MODE_SET、未知消息错误应答、版本拒绝。
"""
import atexit
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
import ctlmsg_generated as ctl

HEADER = struct.Struct('<BBHI')   # version, msg_type, flags, req_id


def encode(msg_type, req_id, body=b'', flags=0x0001):
    return HEADER.pack(ctl.MOZART_CTL_VERSION, msg_type, flags, req_id) + body


def decode(datagram):
    version, msg_type, flags, req_id = HEADER.unpack_from(datagram, 0)
    return version, msg_type, flags, req_id, datagram[HEADER.size:]


class ControlProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='mozart-ctl-')
        atexit.register(cls.directory.cleanup)
        cls.socket_path = str(Path(cls.directory.name) / 'control.sock')
        binary = os.environ.get('MOZART_CONTROL_FIXTURE')
        if not binary:
            root = Path(__file__).resolve().parents[2]
            binary = root / 'build/host/api/control_fixture'
        cls.process = subprocess.Popen([str(binary), cls.socket_path],
                                       stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while not os.path.exists(cls.socket_path) and time.monotonic() < deadline:
            time.sleep(0.05)
        atexit.register(cls.process.kill)

    def client(self):
        # Unix 数据报需要客户端绑定自身地址，服务端才能回包。
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        client_path = str(Path(self.directory.name) / f'c{time.monotonic_ns()}.sock')
        sock.bind(client_path)
        self.addCleanup(lambda: (sock.close(), os.unlink(client_path)))
        sock.settimeout(2)
        return sock

    def test_ping_pong(self):
        sock = self.client()
        sock.sendto(encode(ctl.MOZART_CTL_PING, 42), self.socket_path)
        version, msg_type, flags, req_id, body = decode(sock.recv(4096))
        self.assertEqual((version, msg_type, req_id), (1, ctl.MOZART_CTL_PONG, 42))
        self.assertEqual(flags & 0x0002, 0x0002)
        self.assertEqual(body, b'')

    def test_status_maps_mode_and_counters(self):
        sock = self.client()
        # 夹具模式是共享状态：先归位再查询，消除用例间顺序耦合
        sock.sendto(encode(ctl.MOZART_CTL_MODE_SET, 6,
                           bytes([ctl.MOZART_CTL_MODE_REALTIME])), self.socket_path)
        sock.recv(4096)
        sock.sendto(encode(ctl.MOZART_CTL_STATUS, 7), self.socket_path)
        version, msg_type, flags, req_id, body = decode(sock.recv(4096))
        self.assertEqual(msg_type, ctl.MOZART_CTL_STATUS_DATA)
        mode, state = body[0], body[1]
        finished, failed = struct.unpack_from('<II', body, 2)
        self.assertEqual(mode, ctl.MOZART_CTL_MODE_REALTIME)
        self.assertEqual(state, ctl.MOZART_CTL_STATE_READY)
        self.assertEqual((finished, failed), (0, 0))

    def test_mode_set_accepted(self):
        sock = self.client()
        sock.sendto(encode(ctl.MOZART_CTL_MODE_SET, 9, bytes([ctl.MOZART_CTL_MODE_FILE])),
                    self.socket_path)
        version, msg_type, flags, req_id, body = decode(sock.recv(4096))
        self.assertEqual(msg_type, ctl.MOZART_CTL_MODE_EVT)
        self.assertEqual(body[0], ctl.MOZART_CTL_MODE_FILE)
        # 状态随之切换
        sock.sendto(encode(ctl.MOZART_CTL_STATUS, 10), self.socket_path)
        _, msg_type, _, _, body = decode(sock.recv(4096))
        self.assertEqual(msg_type, ctl.MOZART_CTL_STATUS_DATA)
        self.assertEqual(body[0], ctl.MOZART_CTL_MODE_FILE)

    def test_unknown_message_gets_error_not_silence(self):
        sock = self.client()
        sock.sendto(encode(99, 11), self.socket_path)
        version, msg_type, flags, req_id, body = decode(sock.recv(4096))
        self.assertEqual(msg_type, ctl.MOZART_CTL_ERR)
        code = struct.unpack_from('<I', body, 0)[0]
        self.assertEqual(code, ctl.MOZART_CTL_ERR_UNKNOWN_MSG)
        self.assertEqual(req_id, 11)

    def test_bad_version_rejected(self):
        sock = self.client()
        sock.sendto(HEADER.pack(9, ctl.MOZART_CTL_PING, 1, 5), self.socket_path)
        _, msg_type, _, _, body = decode(sock.recv(4096))
        self.assertEqual(msg_type, ctl.MOZART_CTL_ERR)

    def test_generated_constants_match_header(self):
        # 契约单源：生成器输出必须与仓内生成物一致
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[2] / 'tools/gen_ctlmsg.py')],
            capture_output=True, text=True, check=True)
        committed = (Path(__file__).resolve().parents[2] / 'tools/ctlmsg_generated.py').read_text()
        self.assertEqual(result.stdout.replace('\n\n', '\n').strip(),
                         committed.replace('\n\n', '\n').strip())


if __name__ == '__main__':
    unittest.main()
