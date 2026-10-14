#!/usr/bin/env python3
"""测量隔离后端的控制接口、音频往返和文件转换延迟。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request

import numpy as np
import soundfile as sf

from run_efficiency_campaign import free_port, stats


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--template', type=Path, required=True)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--generator', type=Path, required=True)
    parser.add_argument('--generator-engine', type=Path)
    parser.add_argument('--file-repeats', type=int, default=5)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    models = root/'models'
    for model_id in ['a-rt', 'b-rt']:
        directory = models/model_id
        directory.mkdir(parents=True)
        original = args.assets/'qiqi-zh-realtime'
        for path in original.iterdir():
            if path.suffix in {'.onnx', '.engine'} or path.name == 'config.json':
                (directory/path.name.replace('qiqi-zh-realtime', model_id)).symlink_to(path.resolve())
    directory = models/'quality'
    directory.mkdir()
    (directory/'quality.onnx').symlink_to(args.generator.resolve())
    if args.generator_engine:
        (directory/'quality.engine').symlink_to(args.generator_engine.resolve())
    (directory/'config.json').write_bytes((args.assets/'config.json').read_bytes())
    config = json.loads(args.template.read_text())
    port, udp = free_port(socket.SOCK_STREAM), free_port(socket.SOCK_DGRAM)
    config['network']['control']['port'] = port
    config['network']['audio']['port'] = udp
    config['rvc']['models_dir'] = str(models)
    config['storage']['temp_dir'] = str(root/'temp')
    config['storage']['presets_path'] = str(root/'presets.json')
    save(root/'backend.json', config)
    records = []
    report = dict(binary_sha256=hashlib.sha256(args.binary.read_bytes()).hexdigest(),
                  input_sha256=hashlib.sha256(args.input.read_bytes()).hexdigest(),
                  clock='Python monotonic_ns；HTTP 从 urlopen 前计时，到完整响应正文读取结束',
                  config=config, records=records)

    def api(route, body=None, method=None, label=None, content_type='application/json'):
        payload = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f'http://127.0.0.1:{port}{route}', data=payload,
            method=method, headers={'Content-Type': content_type})
        began = time.monotonic_ns()
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                raw, code = response.read(), response.status
        except urllib.error.HTTPError as error:
            raw, code = error.read(), error.code
        elapsed = (time.monotonic_ns()-began)/1e6
        if label:
            records.append(dict(feature=label, elapsed_ms=elapsed, status=code, bytes=len(raw)))
        if code >= 400:
            raise RuntimeError(f'{route}: {code}: {raw[:400]!r}')
        return json.loads(raw) if not raw.startswith(b'RIFF') else raw

    def repeat(label, route, body=None, method=None, count=20):
        for _ in range(count):
            api(route, body, method, label)

    def switch(mode, model='a-rt', label=None):
        return api('/api/mode/switch', dict(mode=mode, model_id=model), label=label)

    environment = {**os.environ, 'MOZART_RVC_PROFILE': '1'}
    began = time.monotonic()
    with (root/'backend.log').open('w') as log:
        process = subprocess.Popen([str(args.binary), str(root/'backend.json')], stdout=log,
            stderr=subprocess.STDOUT, env=environment, start_new_session=True)
        try:
            deadline = time.monotonic()+90
            while True:
                if process.poll() is not None:
                    raise RuntimeError('后端启动失败')
                try:
                    initial = api('/api/status')
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError('后端启动超时')
                    time.sleep(.02)
            report['startup_ms'] = (time.monotonic()-began)*1000
            save(root/'initial-status.json', initial)
            for route in ['health', 'status', 'monitor', 'models', 'parameters', 'presets', 'logs']:
                repeat('GET '+route, '/api/'+route)
            repeat('PUT parameters', '/api/parameters', {'pitch_shift': 0}, 'PUT')
            repeat('reset parameters', '/api/parameters/reset', {}, 'POST')
            repeat('routing', '/api/realtime/routing', {'mic_muted': False, 'bypass': False})
            for index in range(10):
                preset = api('/api/presets', {'name': f'latency-{index}'}, label='save preset')
                key = preset.get('id', preset.get('preset', {}).get('id'))
                if not key:
                    raise RuntimeError(f'预设没有标识符：{preset}')
                api('/api/presets/'+key, method='DELETE', label='delete preset')
            switch('idle', label='idle already idle')
            api('/api/models/b-rt/activate', {}, label='uncached model activate')
            for _ in range(10):
                api('/api/models/a-rt/activate', {}, label='cached model activate')
                api('/api/models/b-rt/activate', {}, label='cached model activate')
            for _ in range(10):
                switch('rt_rvc', 'a-rt', 'start realtime worker cached')
                switch('idle', label='stop realtime worker')
            header = struct.Struct('<IQIBBBB')
            audio, rate = sf.read(args.input, dtype='float32')
            assert rate == 16000 and audio.ndim == 1
            frame_count = (len(audio)+319)//320
            streams = {}
            for name, routing in [('rvc', {}), ('bypass', {'bypass': True}), ('mute', {'mic_muted': True})]:
                switch('rt_rvc')
                api('/api/realtime/routing', {'bypass': False, 'mic_muted': False, **routing})
                values, output, first_nonzero = [], [], None
                start = time.monotonic()
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(2)
                    for index in range(frame_count+150):
                        wait = start+index*.02-time.monotonic()
                        if wait > 0:
                            time.sleep(wait)
                        sent = time.monotonic_ns()
                        frame = np.zeros(320, dtype='<f4')
                        part = audio[index*320:(index+1)*320]
                        frame[:len(part)] = part
                        payload = header.pack(0x4D5A5254, sent, index, int(index < frame_count), 220, 255, 1)+frame.tobytes()
                        sock.sendto(payload, ('127.0.0.1', udp))
                        data, _ = sock.recvfrom(65536)
                        elapsed = (time.monotonic_ns()-sent)/1e6
                        if len(data) != header.size+960*4 or header.unpack_from(data)[2] != index:
                            raise RuntimeError('音频契约帧不匹配')
                        samples = np.frombuffer(data, dtype='<f4', offset=header.size).copy()
                        if first_nonzero is None and np.max(np.abs(samples)) > 1e-6:
                            first_nonzero = index*20
                        values.append(elapsed)
                        output.append(samples)
                save(root/(name+'-udp.json'), values)
                sf.write(root/(name+'-output.wav'), np.concatenate(output), 48000, subtype='FLOAT')
                streams[name] = dict(roundtrip=stats(values), first_nonzero_frame_ms=first_nonzero,
                                     status=api('/api/status'))
                switch('idle')
            report['streams'] = streams
            switch('file_rvc', 'quality', 'activate quality model and file mode')
            repeat('pause file queue', '/api/file/pause', {}, 'POST')
            boundary = 'MozartFeatureLatencyBoundary'
            body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model_id"\r\n\r\nquality\r\n'
                    f'--{boundary}\r\nContent-Disposition: form-data; name="audio_file"; filename="input.wav"\r\n'
                    'Content-Type: audio/wav\r\n\r\n').encode()+args.input.read_bytes()+f'\r\n--{boundary}--\r\n'.encode()
            def submit():
                return api('/api/file/convert', body, label='upload and enqueue file',
                           content_type='multipart/form-data; boundary='+boundary)['job_id']
            cancelled = submit()
            api('/api/file/cancel?job_id='+cancelled, method='DELETE', label='cancel queued file')
            api('/api/file/job?job_id='+cancelled, method='DELETE', label='delete cancelled file')
            repeat('resume file queue', '/api/file/resume', {}, 'POST')
            files = []
            for index in range(args.file_repeats):
                started = time.monotonic()
                key = submit()
                while True:
                    status = api('/api/file/status?job_id='+key, label='file status')
                    if status['status'] in ['completed', 'failed', 'cancelled']:
                        break
                    if time.monotonic()-started > 120:
                        raise TimeoutError('文件转换超时')
                    time.sleep(.02)
                elapsed = (time.monotonic()-started)*1000
                if status['status'] != 'completed':
                    raise RuntimeError(f'文件转换失败：{status}')
                raw = api('/api/file/result?job_id='+key, label='download file result')
                (root/f'file-{index}.wav').write_bytes(raw)
                files.append(dict(index=index, upload_to_completed_observed_ms=elapsed, job=status,
                                  wav_sha256=hashlib.sha256(raw).hexdigest()))
                api('/api/file/job?job_id='+key, method='DELETE', label='delete completed file')
            repeat('clear finished files', '/api/file/finished', method='DELETE')
            repeat('clear logs', '/api/logs', method='DELETE')
            report['files'] = files
            report['final_status'] = api('/api/status')
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            report['process_exit_code'] = process.returncode
            report['summary'] = {key: stats([r['elapsed_ms'] for r in records if r['feature'] == key])
                                 for key in sorted({r['feature'] for r in records})}
            save(root/'results.json', report)
    print(json.dumps(report['summary'], indent=2))


if __name__ == '__main__':
    main()
