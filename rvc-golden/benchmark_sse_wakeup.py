#!/usr/bin/env python3
"""用同一追加协议比较字幕文件到 SSE 客户端的延迟。"""
import argparse
import json
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import threading
import time
import urllib.request

from run_efficiency_campaign import free_port, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--template', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    result = {}
    for name, binary in [('baseline', args.baseline), ('candidate', args.candidate)]:
        directory = args.output/name
        directory.mkdir()
        path = directory/'captions.jsonl'
        path.write_text('')
        config = json.loads(args.template.read_text())
        port = free_port(socket.SOCK_STREAM)
        config['network']['control']['port'] = port
        config['network']['audio']['port'] = free_port(socket.SOCK_DGRAM)
        config['rvc']['enabled'] = False
        config['storage']['temp_dir'] = str(directory/'temp')
        config['storage']['presets_path'] = str(directory/'presets.json')
        backend = directory/'backend.json'
        backend.write_text(json.dumps(config, indent=2)+'\n')
        with (directory/'backend.log').open('w') as log:
            process = subprocess.Popen([str(binary), str(backend)], stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, 'MOZART_SUBTITLES_JSONL': str(path)}, start_new_session=True)
            response = None
            try:
                deadline = time.monotonic()+30
                while True:
                    try:
                        response = urllib.request.urlopen(f'http://127.0.0.1:{port}/api/subtitles', timeout=3)
                        break
                    except OSError:
                        if time.monotonic() > deadline or process.poll() is not None:
                            raise RuntimeError('字幕测量服务启动失败')
                        time.sleep(.02)
                received = queue.Queue()
                def reader():
                    try:
                        for raw in response:
                            if raw.startswith(b'data:'):
                                received.put((json.loads(raw[5:]), time.monotonic_ns()))
                    except OSError:
                        pass
                thread = threading.Thread(target=reader, daemon=True)
                thread.start()
                time.sleep(.05)
                elapsed = []
                for index in range(100):
                    time.sleep(.005+(index%13)*.0013)
                    began = time.monotonic_ns()
                    with path.open('a') as output:
                        output.write(json.dumps(dict(seq=index, started_ns=began))+'\n')
                    item, seen = received.get(timeout=3)
                    if item['seq'] != index:
                        raise RuntimeError('字幕记录丢失或顺序改变')
                    elapsed.append((seen-began)/1e6)
                result[name] = dict(summary=stats(elapsed), elapsed_ms=elapsed,
                    clock='单调时钟；文件追加前→Python SSE 客户端读取完整 data 行')
            finally:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
                if response:
                    response.close()
    (args.output/'results.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({name: value['summary'] for name, value in result.items()}, indent=2))


if __name__ == '__main__':
    main()
