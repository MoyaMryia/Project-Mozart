#!/usr/bin/env python3
"""控制台静态服务：提供 frontend/dist 并把接口转发到本机 API。

浏览器只访问同一个地址，前端不需要再配置代理，也不受跨域限制。
示例：python3 tools/serve-console.py --port 8080
"""
import argparse
import http.client
import pathlib
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

PROXY_PREFIXES = ('/api/', '/health', '/status', '/models')

# 上游默认地址只定义这一处；--api 省略端口时沿用它。
DEFAULT_UPSTREAM = ('127.0.0.1', 18080)


def is_proxy(path):
    return any(path == prefix.rstrip('/') or path.startswith(prefix) for prefix in PROXY_PREFIXES)


class Handler(BaseHTTPRequestHandler):
    # 响应用 HTTP/1.0：未声明长度的正文以连接关闭定界。
    protocol_version = 'HTTP/1.0'
    server_version = 'MozartConsole/1.0'
    dist = pathlib.Path('frontend/dist')
    upstream = DEFAULT_UPSTREAM

    def log_message(self, fmt, *args):
        sys.stderr.write('%s %s\n' % (self.address_string(), fmt % args))

    def do_GET(self):
        self.handle_request('GET')

    def do_POST(self):
        self.handle_request('POST')

    def do_PUT(self):
        self.handle_request('PUT')

    def do_DELETE(self):
        self.handle_request('DELETE')

    def handle_request(self, method):
        path = urlsplit(self.path).path
        if is_proxy(path):
            self.proxy(method)
        else:
            self.serve_static(path)

    # --- 静态文件 ---
    def serve_static(self, path):
        relative = path.lstrip('/') or 'index.html'
        target = (self.dist/relative).resolve()
        if self.dist.resolve() not in target.parents and target != self.dist.resolve():
            self.send_error(403, 'path outside dist')
            return
        if not target.is_file():
            # 单页应用：未知路径交回 index.html。
            target = self.dist/'index.html'
            if not target.is_file():
                self.send_error(404, 'frontend/dist 不存在，先运行 npm --prefix frontend run build')
                return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', self.content_type(target))
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def content_type(path):
        suffix = path.suffix.lower()
        return {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                '.css': 'text/css; charset=utf-8', '.json': 'application/json',
                '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon',
                '.woff2': 'font/woff2'}.get(suffix, 'application/octet-stream')

    # --- 转发 ---
    def proxy(self, method):
        request_length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(request_length) if request_length else None
        connection = http.client.HTTPConnection(*self.upstream, timeout=600)
        headers = {key: value for key, value in self.headers.items()
                   if key.lower() not in ('host', 'connection', 'accept-encoding')}
        headers['Accept-Encoding'] = 'identity'
        headers_sent = False
        try:
            connection.request(method, self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status, response.reason)
            streaming = response.getheader('Content-Type', '').startswith('text/event-stream')
            for key, value in response.getheaders():
                if key.lower() in ('transfer-encoding', 'connection', 'content-length'):
                    continue
                self.send_header(key, value)
            # 上游未声明长度时不能写 Content-Length: 0，否则正文被截断。
            # 此时声明 close，浏览器以连接关闭作为正文结束。
            if streaming or response.length is None:
                self.send_header('Connection', 'close')
            else:
                self.send_header('Content-Length', str(response.length))
            self.end_headers()
            headers_sent = True
            # 流式不能用缓冲读：它会攒满块长度才返回，字幕事件因此成批到达。
            read = response.read1 if streaming else response.read
            chunk_size = 4096 if streaming else 65536
            while True:
                chunk = read(chunk_size)
                if not chunk:
                    break
                self.wfile.write(chunk)
                if streaming:
                    self.wfile.flush()
        except (OSError, http.client.HTTPException) as error:
            if headers_sent:
                # 状态行和响应头已发出，不能再写错误响应，否则正文被污染。
                self.log_message('transfer aborted after headers: %s', error)
                self.close_connection = True
            else:
                # send_error 只能写 ASCII；原因写日志，响应体保持简短。
                self.log_message('upstream failed: %s', error)
                self.send_error(502, 'upstream unreachable')
        finally:
            connection.close()


def main():
    parser = argparse.ArgumentParser(description='Mozart 控制台静态服务')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--dist', type=pathlib.Path, default=pathlib.Path('frontend/dist'))
    parser.add_argument('--api', default='%s:%d' % DEFAULT_UPSTREAM,
                        help='上游 API，host:port')
    arguments = parser.parse_args()

    Handler.dist = arguments.dist
    host, _, port = arguments.api.partition(':')
    Handler.upstream = (host, int(port or DEFAULT_UPSTREAM[1]))
    if not (Handler.dist/'index.html').is_file():
        raise SystemExit(f'缺少构建产物：{Handler.dist}/index.html，先运行 npm --prefix frontend run build')

    server = ThreadingHTTPServer((arguments.host, arguments.port), Handler)
    server.daemon_threads = True
    print(f'控制台 http://{arguments.host}:{arguments.port}/ ，接口转发到 {arguments.api}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
