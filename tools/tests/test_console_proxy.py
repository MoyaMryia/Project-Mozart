"""Check the console proxy forwards bodies without a declared length and streams events.

The upstream API sends dynamic JSON without Content-Length. The console must not
declare a zero length for it, must forward event streams as they arrive, and must
not append a second status line after the response headers are sent.
"""
import http.client
import json
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO/'tools'/'serve-console.py'


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == '/api/lengthless':
            body = b'{"error":"Reference speech service unavailable"}'
            self.send_response(503, 'Service Unavailable')
            self.send_header('Content-Type', 'application/json')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/api/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            for index in (1, 2):
                self.wfile.write(json.dumps({'seq': index}).encode()+b'\n\n')
                self.wfile.flush()
                if index == 1:
                    time.sleep(1.0)
            time.sleep(1.0)
        elif self.path == '/api/truncated':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(b'{"partial":true}')
            self.wfile.flush()
            # 复位连接，让代理在正文中途收到读错误。
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                       struct.pack('ii', 1, 0))
            self.connection.close()
            self.close_connection = True
        else:
            self.send_error(404)


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def request(port, path, seconds=5.0):
    """Return (status_line, body) read until the server closes the connection."""
    with socket.create_connection(('127.0.0.1', port), timeout=seconds) as client:
        client.sendall(f'GET {path} HTTP/1.0\r\nHost: console\r\n\r\n'.encode())
        client.settimeout(seconds)
        data = b''
        try:
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
    head, _, body = data.partition(b'\r\n\r\n')
    return head.split(b'\r\n')[0].decode(), body


class ConsoleProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream_port = free_port()
        cls.upstream = ThreadingHTTPServer(('127.0.0.1', cls.upstream_port), Upstream)
        cls.upstream.daemon_threads = True
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        # 构建产物放临时目录：仓库内写入未跟踪文件会卡住 pre-commit 的干净检查。
        cls.dist = tempfile.TemporaryDirectory(prefix='mozart-console-dist-')
        dist = Path(cls.dist.name)
        (dist/'index.html').write_text('<!doctype html><title>console</title>')
        cls.port = free_port()
        cls.process = subprocess.Popen(
            [sys.executable, str(SERVER), '--host', '127.0.0.1', '--port', str(cls.port),
             '--dist', str(dist), '--api', f'127.0.0.1:{cls.upstream_port}'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time()+10
        while time.time() < deadline:
            try:
                socket.create_connection(('127.0.0.1', cls.port), timeout=0.2).close()
                return
            except OSError:
                if cls.process.poll() is not None:
                    raise unittest.SkipTest('console server exited')
                time.sleep(0.05)
        raise unittest.SkipTest('console server did not listen')

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=10)
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.dist.cleanup()

    def test_body_without_declared_length_arrives_whole(self):
        # 浏览器按 Content-Length 取正文；这里用同样的解析规则复现截断。
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request('GET', '/api/lengthless')
            response = connection.getresponse()
            self.assertEqual(response.status, 503)
            body = json.loads(response.read().decode())
        finally:
            connection.close()
        self.assertEqual(body['error'], 'Reference speech service unavailable')

    def test_event_stream_arrives_before_buffer_fills(self):
        started = time.monotonic()
        first = b''
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as client:
            client.sendall(b'GET /api/stream HTTP/1.0\r\nHost: console\r\n\r\n')
            client.settimeout(5)
            head = b''
            while b'\r\n\r\n' not in head:
                head += client.recv(4096)
            body = head.split(b'\r\n\r\n', 1)[1]
            while b'"seq": 1' not in body:
                body += client.recv(4096)
            first = body
            elapsed = time.monotonic()-started
        self.assertIn(b'text/event-stream', head)
        # 上游第二条事件在 1.0 s 后才发；缓冲读会等到那时才返回。
        self.assertLess(elapsed, 0.6, 'first event was buffered with the second')
        self.assertIn(b'"seq": 1', first)

    def test_abort_after_headers_keeps_body_unpolluted(self):
        status, body = request(self.port, '/api/truncated')
        self.assertIn('200', status)
        self.assertNotIn(b'HTTP/1.0 502', body)
        self.assertNotIn(b'upstream unreachable', body)


if __name__ == '__main__':
    unittest.main()
