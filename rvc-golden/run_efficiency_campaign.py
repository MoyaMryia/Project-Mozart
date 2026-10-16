#!/usr/bin/env python3
"""Compare isolated realtime backends with fixed assets and identical input."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import numpy as np


def request(port):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status', timeout=2) as response:
        return json.load(response)


def free_port(kind):
    with socket.socket(socket.AF_INET, kind) as reserve:
        reserve.bind(('127.0.0.1', 0))
        return reserve.getsockname()[1]


def stats(values):
    return dict(count=len(values), median_ms=float(np.median(values)),
                p95_ms=float(np.percentile(values, 95)), max_ms=float(max(values)))


def process_memory(pid):
    text = Path(f'/proc/{pid}/status').read_text()
    return {key: int(re.search(rf'^{key}:\s+(\d+)', text, re.M)[1])*1024 for key in ['VmRSS', 'VmHWM']}


def validate_cases(cases):
    allowed = {'baseline', 'compatible', 'chain', 'parallel'}
    if any(case not in allowed for case in cases):
        raise ValueError('Invalid cases')
    if 'baseline' not in cases or len(set(cases)) < 2:
        raise ValueError('Cases must include baseline and at least one different case')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--playout-delay-ms', type=int, default=0)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--cases', nargs='+', default=['baseline', 'compatible', 'chain', 'parallel'])
    mode.add_argument('--measure-case', choices=['baseline', 'compatible', 'chain', 'parallel'])
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('Invalid repeats or cases')
    if args.measure_case:
        args.cases = [args.measure_case]
    else:
        try:
            validate_cases(args.cases)
        except ValueError as error:
            parser.error(str(error))
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    model_id = 'qiqi-zh-realtime'
    model_root = root/'models'/model_id
    model_root.mkdir(parents=True, exist_ok=True)
    for asset in (args.assets/model_id).iterdir():
        if asset.suffix in {'.onnx', '.engine'} or asset.name == 'config.json':
            link = model_root/asset.name
            if not link.exists():
                link.symlink_to(asset.resolve())
    report = dict(input=str(args.input.resolve()), input_sha256=hashlib.sha256(args.input.read_bytes()).hexdigest(),
        clock='Backend steady_clock around realtime inference; all blocks including flush; constructor warmup excluded',
        assets={str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in model_root.iterdir()}, runs=[])
    all_audio = []
    for repeat in range(args.repeats):
        # 顺序交替，降低固定执行顺序对温度和缓存的影响。
        cases = args.cases if repeat % 2 == 0 else list(reversed(args.cases))
        for case in cases:
            directory = root/f'{case}-{repeat+1}'
            directory.mkdir(exist_ok=False)
            http_port, udp_port = free_port(socket.SOCK_STREAM), free_port(socket.SOCK_DGRAM)
            config = dict(input={'contract': {'sample_rate': 16000}, 'meta': {'vad_enabled': False}},
                output={'sample_rate': 48000}, network={'audio': {'host': '127.0.0.1', 'port': udp_port, 'frame_duration_ms': 20},
                'control': {'host': '127.0.0.1', 'port': http_port}},
                rvc=dict(models_dir=str(model_root.parent), hubert_path=str(args.assets/'hubert_base_dynamic.onnx'),
                    rmvpe_path=str(args.assets/'rmvpe_dynamic.onnx'), realtime_hubert_path=str(args.assets/'hubert-realtime.onnx'),
                    realtime_rmvpe_path=str(args.assets/'rmvpe-realtime.onnx'), f0_method='rmvpe', pitch_shift=0, index_rate=0.,
                    filter_radius=3, rms_mix_rate=1., protect=.33, device='cuda', half=False,
                    realtime_playout_delay_ms=args.playout_delay_ms,
                    mock=dict(generator=False, hubert=False, rmvpe=False)),
                storage=dict(temp_dir=str(directory/'temp'), presets_path=str(directory/'presets.json'),
                    file_rnnoise=False, ffmpeg_path='ffmpeg', max_queue_depth=5))
            config_path = directory/'backend.json'
            config_path.write_text(json.dumps(config, indent=2)+'\n')
            environment = {**os.environ, 'MOZART_RVC_PROFILE': '1',
                'MOZART_RVC_DEVICE_CHAIN': '1' if case in {'chain', 'parallel'} else '0',
                'MOZART_RVC_PARALLEL_FEATURES': '1' if case == 'parallel' else '0'}
            binary = args.baseline if case == 'baseline' else args.candidate
            with (directory/'backend.log').open('w') as log:
                started = time.monotonic()
                process = subprocess.Popen([str(binary), str(config_path)], stdout=log, stderr=subprocess.STDOUT,
                                           env=environment, start_new_session=True)
                try:
                    deadline = time.monotonic()+60
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError(f'Backend exited: {directory}')
                        try:
                            request(http_port)
                            break
                        except (OSError, ValueError):
                            if time.monotonic() >= deadline:
                                raise RuntimeError(f'Backend startup timed out: {directory}')
                            time.sleep(.1)
                    startup_seconds = time.monotonic()-started
                    ready_memory = process_memory(process.pid)
                    output = directory/f'qiqi-espeak-{case}-mozart-backend.wav'
                    with (directory/'client.log').open('w') as client_log:
                        subprocess.run([sys.executable, str(args.repo/'tools/stream_audio_udp.py'), str(args.input), str(output),
                            '--api', f'http://127.0.0.1:{http_port}', '--backend', f'127.0.0.1:{udp_port}',
                            '--model-id', model_id, '--flush-seconds', '3', '--trim-leading-underruns', '--trim-to-rvc-duration'],
                            stdout=client_log, stderr=subprocess.STDOUT, check=True, timeout=180)
                    status = request(http_port)
                    final_memory = process_memory(process.pid)
                    (directory/'status.json').write_text(json.dumps(status, indent=2)+'\n')
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
            import soundfile as sf
            audio, rate = sf.read(output, dtype='float32')
            text = (directory/'backend.log').read_text()
            times = [float(value) for value in re.findall(r'\[profile\]\[realtime\][^\n]* total=([0-9.]+)ms', text)]
            if not times or not np.isfinite(audio).all():
                raise RuntimeError('Missing inference timing or non-finite audio')
            stream = status['stream']
            validation_errors = [key for key in ['inference_errors', 'late_blocks', 'input_overruns', 'output_overruns', 'resets']
                                 if stream.get(key)]
            if stream['output_underruns'] != stream['startup_output_underruns']:
                validation_errors.append('steady_state_output_underrun')
            all_audio.append(audio)
            record = dict(case=case, repeat=repeat+1, inference=stats(times), stream=stream, validation_errors=validation_errors,
                startup_seconds=startup_seconds, ready_process_memory=ready_memory, final_process_memory=final_memory,
                first_frame_ms=stream['startup_output_underruns']*20, samples=len(audio), sample_rate=rate,
                peak=float(np.max(np.abs(audio))), rms=float(np.sqrt(np.mean(audio.astype(np.float64)**2))),
                clip_samples=int(np.count_nonzero(np.abs(audio) >= 1)), pcm_sha256=hashlib.sha256(audio.tobytes()).hexdigest())
            report['runs'].append(record)
            (root/'campaign.json').write_text(json.dumps(report, indent=2)+'\n')
            print(json.dumps(record), flush=True)
            if validation_errors:
                raise RuntimeError(f'Realtime validation failed: {validation_errors}')
    report['comparison_performed'] = args.measure_case is None
    report['all_pcm_identical'] = (all(np.array_equal(all_audio[0], audio) for audio in all_audio[1:])
                                   if report['comparison_performed'] else None)
    (root/'campaign.json').write_text(json.dumps(report, indent=2)+'\n')
    if report['comparison_performed'] and not report['all_pcm_identical']:
        raise RuntimeError('Backend PCM differs across optimization cases')


if __name__ == '__main__':
    main()
