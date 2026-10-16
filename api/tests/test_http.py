"""验证原生 HTTP 连接生命周期，不使用模型或 GPU。"""
import os
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

EXECUTABLE = str(Path(sys.argv.pop(1)).resolve())


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / 'result.wav'
        self.payload = bytes(range(256)) * (24 * 1024)
        self.output.write_bytes(self.payload)
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            self.port = reserve.getsockname()[1]
        env = dict(os.environ, MOZART_SUBTITLES_JSONL=str(Path(self.temporary.name) / 'captions.jsonl'))
        self.process = subprocess.Popen([EXECUTABLE, str(self.port), str(self.output)],
                                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, env=env)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 3
        while True:
            try:
                self.assertIn(b'200 OK', self.request(b'GET /api/health HTTP/1.1\r\n\r\n'))
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.01)

    def stop(self):
        if self.process.poll() is None:
            self.process.stdin.write(b'\n')
            self.process.stdin.flush()
        try:
            _, errors = self.process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()
            self.fail('HTTP stop exceeded 2 seconds')
        self.assertEqual(self.process.returncode, 0, errors.decode())

    def connect(self):
        client = socket.create_connection(('127.0.0.1', self.port), timeout=2)
        self.addCleanup(client.close)
        return client

    def read_all(self, client):
        result = bytearray()
        while True:
            try:
                data = client.recv(65536)
            except ConnectionResetError:
                break
            if not data:
                break
            result.extend(data)
        return bytes(result)

    def request(self, data):
        with self.connect() as client:
            client.sendall(data)
            return self.read_all(client)

    def test_partial_header_keeps_health_and_stop_available(self):
        client = self.connect()
        client.sendall(b'GET /api/status HTTP/1.1\r\n')
        self.assertIn(b'200 OK', self.request(b'GET /api/health HTTP/1.1\r\n\r\n'))
        self.stop()

    def test_partial_body_keeps_health_and_stop_available(self):
        client = self.connect()
        client.sendall(b'POST /api/mode/switch HTTP/1.1\r\nContent-Length: 100\r\n\r\n{')
        self.assertIn(b'200 OK', self.request(b'GET /api/health HTTP/1.1\r\n\r\n'))
        self.stop()

    def test_trickle_header_has_absolute_deadline(self):
        client = self.connect()
        client.sendall(b'GET /api/health HTTP/1.1\r\nX-Slow: ')
        start = time.monotonic()
        while time.monotonic() - start < .8:
            try:
                client.sendall(b'x')
            except OSError:
                break
            time.sleep(.05)
        self.assertIn(b'408 Request Timeout', self.read_all(client))

    def test_body_deadline(self):
        client = self.connect()
        client.sendall(b'POST /api/mode/switch HTTP/1.1\r\nContent-Length: 100\r\n\r\n{')
        self.assertIn(b'408 Request Timeout', self.read_all(client))

    def test_invalid_framing_is_rejected(self):
        for field in [b'Content-Length: -1', b'Content-Length: 2x',
                      b'Content-Length: 18446744073709551616',
                      b'Content-Length: 0\r\nContent-Length: 0',
                      b'Transfer-Encoding: chunked']:
            with self.subTest(field=field):
                response = self.request(b'GET /api/health HTTP/1.1\r\n' + field + b'\r\n\r\n')
                self.assertIn(b'400 Bad Request', response)

    def test_concurrent_request_memory_is_bounded(self):
        first = self.connect()
        partial = b'POST /api/file/convert HTTP/1.1\r\nContent-Length: 30000\r\n\r\n' + b'x' * 20000
        first.sendall(partial)
        time.sleep(.05)
        self.assertIn(b'503 Service Unavailable', self.request(partial))
        first.close()
        time.sleep(.05)
        self.assertIn(b'200 OK', self.request(b'GET /api/health HTTP/1.1\r\n\r\n'))

    def test_request_size_is_checked_without_overflow(self):
        for size in [110 * 1024 * 1024, 18446744073709551615]:
            response = self.request(f'POST /api/file/convert HTTP/1.1\r\nContent-Length: {size}\r\n\r\n'.encode())
            self.assertIn(b'413 Payload Too Large', response)

    def test_header_size_is_bounded(self):
        response = self.request(b'GET /api/health HTTP/1.1\r\nX: ' + b'x' * 17000)
        self.assertIn(b'431 Request Header Fields Too Large', response)

    def test_deferred_mode_uses_202(self):
        response = self.request(b'POST /api/mode/switch HTTP/1.1\r\nContent-Length: 15\r\n\r\n{"mode":"idle"}')
        self.assertIn(b'202 Accepted', response)

    def test_download_is_complete(self):
        response = self.request(b'GET /api/file/result?job_id=test HTTP/1.1\r\n\r\n')
        header, body = response.split(b'\r\n\r\n', 1)
        self.assertIn(f'Content-Length: {len(self.payload)}'.encode(), header)
        self.assertEqual(body, self.payload)

    def test_slow_download_keeps_control_and_stop_available(self):
        client = self.connect()
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        client.sendall(b'GET /api/file/result?job_id=test HTTP/1.1\r\n\r\n')
        time.sleep(.05)
        self.assertIn(b'200 OK', self.request(b'GET /api/health HTTP/1.1\r\n\r\n'))
        self.stop()

    def test_connection_count_is_bounded(self):
        clients = [self.connect() for _ in range(8)]
        for client in clients:
            client.sendall(b'GET /api/health HTTP/1.1\r\nX: ')
        time.sleep(.05)
        self.assertIn(b'503 Service Unavailable', self.request(b'GET /api/health HTTP/1.1\r\n\r\n'))

    def test_slow_download_deadline_releases_connections(self):
        for _ in range(6):
            client = self.connect()
            client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
            client.sendall(b'GET /api/file/result?job_id=test HTTP/1.1\r\n\r\n')
        time.sleep(1.4)
        for _ in range(6):
            client = self.connect()
            client.sendall(b'GET /api/health HTTP/1.1\r\nX: ')
        self.assertIn(b'200 OK', self.request(b'GET /api/health HTTP/1.1\r\n\r\n'))

    def test_sse_limit_heartbeat_and_disconnect_recovery(self):
        clients = []
        for _ in range(2):
            client = self.connect()
            client.sendall(b'GET /api/subtitles HTTP/1.1\r\n\r\n')
            self.assertIn(b'200 OK', client.recv(4096))
            clients.append(client)
        self.assertIn(b'503 Service Unavailable', self.request(b'GET /api/subtitles HTTP/1.1\r\n\r\n'))
        self.assertIn(b': heartbeat\n\n', clients[0].recv(4096))
        for client in clients:
            client.shutdown(socket.SHUT_RDWR)
            client.close()
        deadline = time.monotonic() + 3
        while True:
            client = self.connect()
            client.sendall(b'GET /api/subtitles HTTP/1.1\r\n\r\n')
            response = client.recv(4096)
            if b'200 OK' in response:
                break
            client.close()
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.1)
        self.assertIn(b'200 OK', self.request(b'GET /api/health HTTP/1.1\r\n\r\n'))

    def test_subtitles_append_incomplete_replace_and_truncate(self):
        path = Path(self.temporary.name)/'captions.jsonl'
        path.write_text('{"old":true}\n')
        client = self.connect()
        client.sendall(b'GET /api/subtitles HTTP/1.1\r\n\r\n')
        stream = client.makefile('rb')
        while stream.readline() != b'\r\n':
            pass
        def event():
            while True:
                line = stream.readline()
                if line.startswith(b'data: '):
                    return json.loads(line[6:])
        with path.open('a') as output:
            output.write('{"seq":1}')
            output.flush()
            time.sleep(.03)
            output.write('\n')
        self.assertEqual(event(), {'seq': 1})
        replacement = path.with_suffix('.new')
        replacement.write_text('{"seq":2}\n')
        replacement.replace(path)
        self.assertEqual(event(), {'seq': 2})
        path.write_text('{"seq":3}\n')
        self.assertEqual(event(), {'seq': 3})
        stream.close()

    def test_subtitles_file_created_after_connection(self):
        client = self.connect()
        client.sendall(b'GET /api/subtitles HTTP/1.1\r\n\r\n')
        stream = client.makefile('rb')
        while stream.readline() != b'\r\n':
            pass
        (Path(self.temporary.name)/'captions.jsonl').write_text('{"created":true}\n')
        while True:
            line = stream.readline()
            if line.startswith(b'data: '):
                self.assertEqual(json.loads(line[6:]), {'created': True})
                break
        stream.close()
        self.stop()


if __name__ == '__main__':
    unittest.main()
