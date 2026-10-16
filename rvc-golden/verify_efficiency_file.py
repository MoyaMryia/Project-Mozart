#!/usr/bin/env python3
"""Verify file output and quality-engine initialization after runtime changes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.parse
import urllib.request

import numpy as np
import soundfile as sf
from run_efficiency_campaign import free_port, request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--config-template', type=Path, required=True)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--generator', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    models = args.output_dir/'models'/'quality'
    models.mkdir(parents=True)
    (models/'quality.onnx').symlink_to(args.generator.resolve())
    (models/'config.json').write_bytes((args.assets/'config.json').read_bytes())
    report, outputs = {'generator': str(args.generator),
        'generator_sha256': hashlib.sha256(args.generator.read_bytes()).hexdigest(),
        'input_sha256': hashlib.sha256(args.input.read_bytes()).hexdigest()}, []
    for name, binary in [('baseline', args.baseline), ('candidate', args.candidate)]:
        directory = args.output_dir/name
        directory.mkdir()
        config = json.loads(args.config_template.read_text())
        config['rvc']['models_dir'] = str(models.parent.resolve())
        config['storage']['temp_dir'] = str((directory/'temp').resolve())
        config['storage']['presets_path'] = str((directory/'presets.json').resolve())
        port = free_port(socket.SOCK_STREAM)
        config['network']['control']['port'] = port
        config['network']['audio']['port'] = free_port(socket.SOCK_DGRAM)
        path = directory/'backend.json'
        path.write_text(json.dumps(config, indent=2)+'\n')
        with (directory/'backend.log').open('w') as log:
            process = subprocess.Popen([str(binary), str(path)], stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            def api(route, data=None, content_type='application/json'):
                req = urllib.request.Request(f'http://127.0.0.1:{port}{route}', data=data,
                    headers={'Content-Type': content_type})
                with urllib.request.urlopen(req, timeout=15) as response:
                    return response.read()
            try:
                deadline = time.monotonic()+60
                while True:
                    if process.poll() is not None:
                        raise RuntimeError('Backend startup failed')
                    try:
                        before = request(port)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise RuntimeError('Backend startup timed out')
                        time.sleep(.1)
                if name == 'candidate' and before['model'].get('hubert_backend') != 'deferred':
                    raise RuntimeError('Quality engines loaded before first use')
                api('/api/mode/switch', json.dumps({'mode': 'file_rvc', 'model_id': 'quality'}).encode())
                boundary = 'MozartEfficiencyFileBoundary'
                body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model_id"\r\n\r\nquality\r\n'
                    f'--{boundary}\r\nContent-Disposition: form-data; name="audio_file"; filename="input.wav"\r\n'
                    'Content-Type: audio/wav\r\n\r\n').encode()+args.input.read_bytes()+f'\r\n--{boundary}--\r\n'.encode()
                submitted = json.loads(api('/api/file/convert', body, f'multipart/form-data; boundary={boundary}'))
                query = urllib.parse.urlencode({'job_id': submitted['job_id']})
                deadline = time.monotonic()+120
                while True:
                    job = json.loads(api('/api/file/status?'+query))
                    if job['status'] in ['completed', 'failed', 'cancelled']:
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError('File inference timed out')
                    time.sleep(.1)
                if job['status'] != 'completed':
                    raise RuntimeError(f'File inference failed: {job}')
                output = directory/'qiqi-espeak-file-mozart-backend.wav'
                output.write_bytes(api('/api/file/result?'+query))
                after = request(port)
                (directory/'status-before.json').write_text(json.dumps(before, indent=2)+'\n')
                (directory/'status-after.json').write_text(json.dumps(after, indent=2)+'\n')
                (directory/'job.json').write_text(json.dumps(job, indent=2)+'\n')
                if name == 'candidate' and after['model'].get('hubert_backend') == 'deferred':
                    raise RuntimeError('Quality engines did not initialize')
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        audio, rate = sf.read(output, dtype='float32')
        if not len(audio) or not np.isfinite(audio).all():
            raise RuntimeError('Invalid file output')
        outputs.append(audio)
        report[name] = dict(sample_rate=rate, samples=len(audio),
            pcm_sha256=hashlib.sha256(audio.tobytes()).hexdigest(),
            peak=float(np.max(np.abs(audio))), rms=float(np.sqrt(np.mean(audio.astype(np.float64)**2))),
            clipped_samples=int(np.count_nonzero(np.abs(audio) >= 1)), model_after=after['model'])
    report['identical_pcm'] = np.array_equal(*outputs)
    (args.output_dir/'verification.json').write_text(json.dumps(report, indent=2)+'\n')
    if not report['identical_pcm']:
        raise RuntimeError('File output differs after optimization')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
