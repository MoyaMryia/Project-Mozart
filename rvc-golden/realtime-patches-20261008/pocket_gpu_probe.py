"""Measure fixed Pocket TTS pieces in one warm worker. Save all results."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024*1024), b''):
            result.update(block)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--texts', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--provider', choices=['cpu', 'cuda'], required=True)
    parser.add_argument('--precision', choices=['int8', 'float32'], required=True)
    parser.add_argument('--pythonpath', type=Path)
    parser.add_argument('--worker-script', type=Path)
    parser.add_argument('--threads', type=int, default=2)
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--early-audio', action='store_true')
    args = parser.parse_args()
    if args.threads < 1:
        parser.error('--threads must be positive')
    sys.path.insert(0, str(args.root/'tools'))
    from speech_chunks import estimated_audio_seconds
    import numpy as np
    import soundfile as sf
    args.output.mkdir(parents=True, exist_ok=False)
    worker = args.worker_script or args.root/'tools/clone_worker.py'
    command = [sys.executable, str(worker), '--engine', 'pocket',
               '--model', str(args.model), '--threads', str(args.threads), '--provider', args.provider,
               '--precision', args.precision]
    if args.early_audio:
        command += ['--early-audio']
    if args.profile:
        config = args.output/'provider.conf'
        config.write_text('LogSeverityLevel=0\nProfilingFilePrefix='+str((args.output/'ort').resolve())+'\n')
        command += ['--provider-config', str(config)]
    environment = os.environ.copy()
    if args.pythonpath:
        environment['PYTHONPATH'] = str(args.pythonpath)+os.pathsep+environment.get('PYTHONPATH', '')
    report = {'command': command, 'worker_sha256': digest(worker), 'reference_sha256': digest(args.reference),
              'texts_sha256': digest(args.texts), 'provider': args.provider,
              'precision': args.precision, 'profile': args.profile, 'records': [],
              'model_sha256': {p.name: digest(p) for p in args.model.glob('*.onnx')}}
    process = None
    def save():
        (args.output/'results.json').write_text(json.dumps(report, indent=2)+'\n')
    def read_result(timeout=120):
        if not select.select([process.stdout], [], [], timeout)[0]:
            raise TimeoutError('Worker response deadline expired')
        line = process.stdout.readline()
        if not line:
            raise RuntimeError('Worker exited without a response')
        return json.loads(line)
    try:
        with (args.output/'worker.log').open('w') as log:
            started = time.monotonic()
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=log, env=environment, cwd=args.root, bufsize=0)
            report['runtime'] = read_result()
            report['startup_seconds'] = time.monotonic()-started
            save()
            for repeat in range(args.repeats):
                for index, text in enumerate(json.loads(args.texts.read_text())):
                    output = args.output/f'{repeat:02d}-{index:02d}.wav'
                    request = {'text': text, 'reference': str(args.reference), 'output': str(output),
                               'live': True, 'chunk_count': 2, 'early_audio': args.early_audio,
                               'max_duration_seconds': min(12, max(4, estimated_audio_seconds(text, 'en')*2.5+2))}
                    started = time.monotonic()
                    process.stdin.write(json.dumps(request).encode()+b'\n')
                    process.stdin.flush()
                    result = read_result()
                    blocks = []
                    while result.get('type') == 'audio':
                        blocks.append({**result, 'observed_seconds': time.monotonic()-started})
                        result = read_result()
                    record = {'repeat': repeat, 'index': index, 'request': request,
                              'roundtrip_seconds': time.monotonic()-started, 'result': result,
                              'audio_blocks': blocks,
                              'first_audio_seconds': blocks[0]['observed_seconds'] if blocks else time.monotonic()-started}
                    if output.exists():
                        pcm, rate = sf.read(output, dtype='float32')
                        record['audio'] = {'sha256': digest(output), 'sample_rate': rate,
                            'samples': len(pcm), 'rms': float(np.sqrt(np.mean(pcm**2))),
                            'peak': float(np.max(np.abs(pcm))), 'finite': bool(np.isfinite(pcm).all())}
                    report['records'].append(record)
                    save()
                    print(json.dumps(record), flush=True)
            process.stdin.close()
            report['exit_code'] = process.wait(timeout=60)
    except Exception as error:
        report['failure'] = str(error)
        raise
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        save()


if __name__ == '__main__':
    main()
