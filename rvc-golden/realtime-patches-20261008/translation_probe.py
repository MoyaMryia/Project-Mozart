"""Compare translation settings on retained source text without audio playback."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('label')
    parser.add_argument('--profile', nargs='+', choices=('original', 'limit-128', 'concise-128'),
                        default=['original', 'limit-128', 'concise-128'])
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root/'tools'))
    import subtitle_bridge as bridge
    lock = (Path.home()/'Mozart/rvc-golden/reliability-20261006/active-test.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with socket.socket() as check:
        check.bind(('127.0.0.1', 18200))
    output = root/'rvc-golden/realtime-patches-20261008'/args.label
    output.mkdir(exist_ok=False)
    source = root/'rvc-golden/realtime-patches-20261008/base-v1/runtime/subtitles.jsonl'
    latest = {}
    for line in source.read_text().splitlines():
        record = json.loads(line)
        if record['final']:
            latest[record['utterance_id']] = record
    model = Path.home()/'mozart-archive/qwen35/gguf/qwen35-0.8b-text-Q4_K_M.gguf'
    command = [str(Path.home()/'mozart-archive/qwen35/llama.cpp/build-cuda-bin/llama-server'),
               '-m', str(model), '-c', '2048', '-b', '256', '-ub', '128',
               '--cache-ram', '128', '-ngl', '99', '-np', '1', '-t', '2', '-tb', '2',
               '--host', '127.0.0.1', '--port', '18200', '--jinja']
    original_request, original_prompt = bridge.request_json, bridge.SYSTEM_PROMPT
    profiles = [('original', 300, original_prompt),
                ('limit-128', 128, original_prompt),
                ('concise-128', 128, original_prompt+' Use concise English. Do not add explanations or repeat the same point.')]
    profiles = [profile for profile in profiles if profile[0] in args.profile]
    result = {'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'bridge_sha256': hashlib.sha256((root/'tools/subtitle_bridge.py').read_bytes()).hexdigest(),
              'command': command, 'profiles': [], 'exit_code': None}
    def save():
        (output/'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    with (output/'translation.log').open('w') as log:
        environment = dict(os.environ)
        environment['LD_LIBRARY_PATH'] = str(Path(command[0]).parent)+':'+environment.get('LD_LIBRARY_PATH', '')
        server = subprocess.Popen(command, cwd=root, env=environment, stdout=log, stderr=subprocess.STDOUT)
        try:
            ready_until = time.monotonic()+60
            while time.monotonic() < ready_until:
                if server.poll() is not None:
                    raise RuntimeError('Translation server exited before readiness')
                try:
                    with urllib.request.urlopen('http://127.0.0.1:18200/health', timeout=1):
                        break
                except OSError:
                    time.sleep(.2)
            else:
                raise RuntimeError('Translation server did not become ready')
            deadline = time.monotonic()+180
            for name, maximum, prompt in profiles:
                profile = {'name': name, 'max_tokens': maximum, 'prompt': prompt, 'items': []}
                result['profiles'].append(profile)
                bridge.SYSTEM_PROMPT = prompt
                for record in sorted(latest.values(), key=lambda row: row['seq']):
                    if time.monotonic() > deadline:
                        raise RuntimeError('Translation comparison exceeded its deadline')
                    item = {'seq': record['seq'], 'zh': record['zh'], 'audit': [], 'requests': []}
                    def request(url, body, timeout=10):
                        started = time.monotonic()
                        response = original_request(url, {**body, 'max_tokens': maximum}, timeout)
                        item['requests'].append({'seconds': time.monotonic()-started,
                                                 'usage': response.get('usage'),
                                                 'timings': response.get('timings')})
                        return response
                    bridge.request_json = request
                    started = time.monotonic()
                    try:
                        item['en'], _ = bridge.translate('http://127.0.0.1:18200', record['zh'], audit=item['audit'])
                        item['status'] = 'completed'
                    except Exception as error:
                        item.update(status='failed', error=str(error))
                    item['seconds'] = time.monotonic()-started
                    profile['items'].append(item)
                    save()
                    print(json.dumps({'profile': name, 'seq': item['seq'], 'status': item['status'],
                                      'seconds': round(item['seconds'], 3)}), flush=True)
        finally:
            bridge.request_json, bridge.SYSTEM_PROMPT = original_request, original_prompt
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
            result['exit_code'] = server.returncode
            save()


if __name__ == '__main__':
    main()
