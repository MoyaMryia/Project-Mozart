#!/usr/bin/env python3
"""比较固定窗口与整段 TensorRT 文件路径，并验证长文件回退。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request

import numpy as np
import soundfile as sf
from run_efficiency_campaign import free_port, stats, process_memory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--legacy', type=Path, required=True)
    parser.add_argument('--cases', nargs='+', default=['baseline-fixed','candidate-fixed','candidate-fast'])
    parser.add_argument('--label', default='file-campaign')
    parser.add_argument('--file-only', action='store_true')
    parser.add_argument('--long-seconds', type=float, default=30)
    args = parser.parse_args()
    if 'candidate-fixed' in args.cases and 'baseline-fixed' not in args.cases:
        parser.error('candidate-fixed requires baseline-fixed for the PCM comparison')
    work = args.work
    output = work/args.label
    output.mkdir(exist_ok=False)
    source = work/'reference/input/qiqi-espeak.wav'
    template = json.loads((work/'feature-latency-native-gpu/backend.json').read_text())
    records, previous_pcm = [], None
    for name, binary, fast in [('baseline-fixed', work/'baseline-mozart_stated', False),
                               ('candidate-fixed', work/'build/state/mozart_stated', False),
                               ('candidate-fast', work/'build/state/mozart_stated', True)]:
        if name not in args.cases:
            continue
        directory = output/name
        model = directory/'models/quality'
        model.mkdir(parents=True)
        for suffix in ['.onnx', '.engine']:
            original = work/'assets'/('qiqi-dynamic-explicit'+suffix) if fast else args.legacy/('qiqi-zh-t398'+suffix)
            (model/('quality'+suffix)).symlink_to(original.resolve())
        (model/'config.json').write_bytes((args.legacy/'config.json').read_bytes())
        config = json.loads(json.dumps(template))
        port = free_port(socket.SOCK_STREAM)
        config['network']['control']['port'] = port
        config['network']['audio']['port'] = free_port(socket.SOCK_DGRAM)
        config['rvc']['models_dir'] = str(model.parent)
        if fast:
            config['rvc']['hubert_path'] = str(work/'assets/hubert-file.onnx')
            config['rvc']['rmvpe_path'] = str(work/'assets/rmvpe-file.onnx')
        if args.file_only:
            config['rvc'].pop('realtime_hubert_path',None)
            config['rvc'].pop('realtime_rmvpe_path',None)
        config['storage']['temp_dir'] = str(directory/'temp')
        config['storage']['presets_path'] = str(directory/'presets.json')
        path = directory/'backend.json'
        path.write_text(json.dumps(config, indent=2)+'\n')
        def api(route, body=None, content_type='application/json'):
            request = urllib.request.Request(f'http://127.0.0.1:{port}{route}',
                data=body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None,
                headers={'Content-Type': content_type})
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read()
            return raw if raw.startswith(b'RIFF') else json.loads(raw)
        with (directory/'backend.log').open('w') as log:
            began = time.monotonic()
            process = subprocess.Popen([str(binary), str(path)], env={**os.environ,'MOZART_RVC_PROFILE':'1'},
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                while True:
                    if process.poll() is not None or time.monotonic()-began > 90:
                        raise RuntimeError('文件测试服务启动失败')
                    try:
                        api('/api/status')
                        break
                    except OSError:
                        time.sleep(.02)
                startup = (time.monotonic()-began)*1000
                api('/api/mode/switch', dict(mode='file_rvc',model_id='quality'))
                sources = [source]*3
                if fast:
                    long_source = output/'qiqi-espeak-repeated-20s-input.wav'
                    subprocess.run(['ffmpeg','-v','error','-y','-stream_loop','-1','-i',str(source),
                        '-t','20','-ar','16000','-ac','1','-c:a','pcm_s16le',str(long_source)], check=True)
                    sources.append(long_source)
                    overflow = output/'qiqi-espeak-repeated-overflow-input.wav'
                    subprocess.run(['ffmpeg','-v','error','-y','-stream_loop','-1','-i',str(source),
                        '-t',str(args.long_seconds),'-ar','16000','-ac','1','-c:a','pcm_s16le',str(overflow)],check=True)
                    sources.extend([overflow,source])
                times, outputs, jobs = [], [], []
                for index, input_path in enumerate(sources):
                    boundary = 'MozartFastFileBoundary'
                    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model_id"\r\n\r\nquality\r\n'
                        f'--{boundary}\r\nContent-Disposition: form-data; name="audio_file"; filename="input.wav"\r\n'
                        'Content-Type: audio/wav\r\n\r\n').encode()+input_path.read_bytes()+f'\r\n--{boundary}--\r\n'.encode()
                    began = time.monotonic()
                    key = api('/api/file/convert', body, 'multipart/form-data; boundary='+boundary)['job_id']
                    minimum_available = float('inf')
                    while True:
                        job = api('/api/file/status?job_id='+key)
                        memory = dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())
                        minimum_available = min(minimum_available, int(memory['MemAvailable'].split()[0])*1024)
                        if job['status'] in ['completed','failed','cancelled']:
                            break
                        if time.monotonic()-began > 120:
                            raise TimeoutError('文件测试超时')
                        time.sleep(.02)
                    elapsed = (time.monotonic()-began)*1000
                    if job['status'] != 'completed':
                        raise RuntimeError(f'文件测试失败：{job}')
                    wav = directory/f'qiqi-espeak-{name}-{index}-mozart-backend.wav'
                    wav.write_bytes(api('/api/file/result?job_id='+key))
                    audio, rate = sf.read(wav, dtype='float32')
                    if not np.isfinite(audio).all() or not len(audio):
                        raise ValueError('文件输出无效')
                    if index < 3:
                        times.append(elapsed)
                        outputs.append(audio)
                    if index == 5 and not np.array_equal(outputs[0], audio):
                        raise ValueError('长文件回退后的短文件输出不同')
                    jobs.append(dict(index=index, source_sha256=hashlib.sha256(input_path.read_bytes()).hexdigest(),
                        observed_ms=elapsed, samples=len(audio), sample_rate=rate,
                        rms=float(np.sqrt(np.mean(audio.astype(np.float64)**2))), peak=float(np.max(abs(audio))),
                        clipped_samples=int(np.count_nonzero(abs(audio)>=1)),
                        minimum_available_bytes=minimum_available, status=api('/api/status')))
                if not all(np.array_equal(outputs[0], audio) for audio in outputs):
                    raise ValueError('重复文件输出不一致')
                if name == 'baseline-fixed': previous_pcm = outputs[0]
                if name == 'candidate-fixed' and not np.array_equal(previous_pcm, outputs[0]):
                    raise ValueError('原固定路径输出发生变化')
                reference = work/'file-fast-final/candidate-fast/qiqi-espeak-candidate-fast-0-mozart-backend.wav'
                if args.file_only and reference.exists():
                    original, _ = sf.read(reference,dtype='float32')
                    if not np.array_equal(original,outputs[0]):
                        raise ValueError('文件专用配置改变了 PCM')
                records.append(dict(case=name, file_only=args.file_only, startup_ms=startup, first_ms=times[0], warm=stats(times[1:]),
                    jobs=jobs, process_memory=process_memory(process.pid), repeated_pcm_identical=True))
                (output/'results.json').write_text(json.dumps(records, indent=2)+'\n')
                print(json.dumps({k:v for k,v in records[-1].items() if k != 'jobs'}), flush=True)
            finally:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=20)


if __name__ == '__main__':
    main()
