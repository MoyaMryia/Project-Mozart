"""验证独立目录中的生产服务启动、模型选择和 HTTP 生命周期。"""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

root = Path(sys.argv[1]).resolve()
assets = Path('/home/moyamryia/Mozart/rvc-golden/qiqi-zh-run')
run = root / 'smoke'
run.mkdir(exist_ok=True)
models = run / 'models'
models.mkdir(exist_ok=True)
invalid = models / 'a-unloadable'
invalid.mkdir(exist_ok=True)
(invalid / 'config.json').write_text('{}')
(invalid / 'a-unloadable.onnx').write_text('intentionally invalid ONNX fixture')
real = models / 'qiqi-zh-realtime'
if real.is_symlink():
    real.unlink()
real.mkdir(exist_ok=True)
for source in (assets / 'qiqi-zh-realtime').iterdir():
    if source.suffix in ('.onnx', '.engine') or source.name == 'config.json':
        target = real / source.name
        if not target.exists():
            target.symlink_to(source)
for port in (19880, 19800):
    with socket.socket(type=socket.SOCK_STREAM if port == 19880 else socket.SOCK_DGRAM) as reserved:
        reserved.bind(('127.0.0.1', port))
config = (assets / 'backend-ui.yaml').read_text()
config = config[config.index('input:'):]
config = config.replace('port: 18080', 'port: 19880').replace('port: 18000', 'port: 19800')
config = config.replace(f'models_dir: {assets}', f'models_dir: {models}')
config = config.replace(f'temp_dir: {assets}/temp', f'temp_dir: {run}/temp')
config = config.replace(f'presets_path: {assets}/presets.json', f'presets_path: {run}/presets.json')
config_path = run / 'backend.yaml'
config_path.write_text(config)


def request(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request('http://127.0.0.1:19880' + path, data=data,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


result = {}
with (run / 'daemon.log').open('w') as log:
    process = subprocess.Popen([str(root / 'build-gpu/state/mozart_stated'), str(config_path)],
                               cwd=root, stdout=log, stderr=subprocess.STDOUT)
    partial = None
    try:
        deadline = time.monotonic() + 90
        while True:
            if process.poll() is not None:
                raise RuntimeError('daemon exited during startup')
            try:
                result['health'] = request('/api/health')
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.1)
        result['status'] = request('/api/status')
        assert result['status']['active_model_id'] == 'qiqi-zh-realtime', result['status']
        result['models'] = request('/api/models')
        result['realtime_start'] = request('/api/mode/switch', {'mode': 'rt_rvc', 'model_id': 'qiqi-zh-realtime'})
        assert result['realtime_start']['status'] == 'active', result['realtime_start']
        result['idle'] = request('/api/mode/switch', {'mode': 'idle'})
        assert result['idle']['status'] == 'active', result['idle']
        partial = socket.create_connection(('127.0.0.1', 19880), timeout=2)
        partial.sendall(b'GET /api/status HTTP/1.1\r\nX-Slow: ')
        start = time.monotonic()
        assert request('/api/health')['status'] == 'ok'
        result['health_during_partial_request_seconds'] = time.monotonic() - start
        start = time.monotonic()
        process.terminate()
        process.wait(timeout=10)
        result['shutdown_with_partial_request_seconds'] = time.monotonic() - start
        assert process.returncode == 0, process.returncode
        result['exit_code'] = process.returncode
        (run / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        if partial is not None:
            partial.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
