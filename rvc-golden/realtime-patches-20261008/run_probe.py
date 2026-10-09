"""Run one bounded replay and retain timing, resource, and shutdown evidence."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import urllib.request


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024*1024), b''):
            result.update(block)
    return result.hexdigest()


def sample(previous):
    cpu = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
    total, idle = sum(cpu), cpu[3]+cpu[4]
    memory = {line.split(':')[0]: int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines()}
    vm = {line.split()[0]: int(line.split()[1]) for line in Path('/proc/vmstat').read_text().splitlines()}
    processes = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            command = (path/'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            if not any(name in command for name in ('mozart_stated', 'mozart-pre', 'speech_service.py', 'clone_worker.py', 'subtitle_bridge.py', 'stt_service.py', 'llama-server')):
                continue
            fields = (path/'stat').read_text().rsplit(')', 1)[1].split()
            status = dict(line.split(':', 1) for line in (path/'status').read_text().splitlines() if ':' in line)
            processes[path.name] = {'ppid': int(fields[1]), 'start_ticks': fields[19],
                'cpu_ticks': int(fields[11])+int(fields[12]), 'command': command,
                'rss_bytes': int(status.get('VmRSS', '0 kB').split()[0])*1024}
        except (OSError, ValueError, IndexError):
            continue
    delta = total-previous.get('total', total)
    return {'at': time.time(), 'total': total, 'idle': idle,
        'cpu_all_cores_percent': 100*(1-(idle-previous['idle'])/delta) if delta else None,
        'available_bytes': memory['MemAvailable'], 'swap_used_bytes': memory['SwapTotal']-memory['SwapFree'],
        'pswpin': vm['pswpin'], 'pswpout': vm['pswpout'], 'processes': processes}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('label')
    parser.add_argument('--mode', choices=('base', 'refine', 'failure'), default='base')
    parser.add_argument('--seconds', type=int, default=120)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--tts-model', type=Path)
    parser.add_argument('--tts-provider', choices=('cpu', 'cuda'), default='cpu')
    parser.add_argument('--tts-precision', choices=('int8', 'float32'), default='int8')
    parser.add_argument('--tts-pythonpath', type=Path)
    parser.add_argument('--tts-threads', type=int, default=2)
    parser.add_argument('--early-audio', action='store_true')
    parser.add_argument('--stable-clauses', action='store_true')
    args = parser.parse_args()
    if args.tts_provider == 'cuda' and args.tts_model is None:
        parser.error('CUDA replay requires --tts-model')
    root = args.root.resolve()
    original = Path.home()/'Mozart'
    output = root/'rvc-golden/realtime-patches-20261008'/args.label
    output.mkdir(parents=True, exist_ok=False)
    lock = (original/'rvc-golden/reliability-20261006/active-test.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    source = original/'rvc-golden/sentence-context-20261007/processed-source-120s.wav'
    reference = original/'rvc-golden/clone-tts/qiqi-20261004/reference-qiqi-natural-source.wav'
    translator = Path.home()/'mozart-archive/qwen35/gguf/qwen35-0.8b-text-Q4_K_M.gguf'
    command = [str(root/'.venv/bin/python'), str(root/'tools/run_translated_speech.py'),
        '--backend-config', str(root/'state/translated-speech.yaml'), '--reference', str(reference),
        '--reference-name', 'Qiqi realtime patch test', '--input', str(source), '--seconds', str(args.seconds),
        '--no-rnnoise', '--playback-device', 'null', '--archive-utterances',
        '--run-dir', str(output/'runtime'), '--data-dir', str(output/'speech')]
    command += ['--tts-provider', args.tts_provider, '--tts-precision', args.tts_precision,
                '--tts-threads', str(args.tts_threads)]
    if args.early_audio:
        command += ['--early-audio']
    if args.stable_clauses:
        command += ['--stable-clauses']
    if args.tts_model:
        command += ['--model', str(args.tts_model)]
    if args.tts_pythonpath:
        command += ['--tts-pythonpath', str(args.tts_pythonpath)]
    if args.mode == 'refine':
        command += ['--final-model', str(Path.home()/'models/sherpa-onnx/clone-test-downloads/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17')]
    save(output/'protocol.json', {'commit': args.commit, 'command': command, 'mode': args.mode,
        'source_sha256': digest(source), 'reference_sha256': digest(reference), 'translator_sha256': digest(translator),
        'tts_model_sha256': {p.name: digest(p) for p in args.tts_model.glob('*.onnx')} if args.tts_model else None,
        'source_note': 'Retained processed 120-second source; no second RNNoise pass; not the original MP4 replay.',
        'output_note': 'Paced ALSA null; no physical-speaker test; RVC disabled.',
        'wall_minus_monotonic_seconds': time.time()-time.monotonic(),
        'source_files': {str(path.relative_to(root)): digest(path) for path in (root/'tools').glob('*.py')},
        'binary_hashes': {name: digest(root/name) for name in ('build-gpu/state/mozart_stated', 'build-gpu/preprocessor/mozart-pre')}})
    stopped = threading.Event()
    def stop(*_):
        stopped.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    def sse_observer():
        with (output/'sse-observed.jsonl').open('w') as stream:
            while not stopped.is_set():
                try:
                    with urllib.request.urlopen('http://127.0.0.1:18080/api/subtitles', timeout=3) as response:
                        while not stopped.is_set():
                            line = response.readline()
                            if not line:
                                break
                            if line.startswith(b'data:'):
                                event = json.loads(line[5:])
                                stream.write(json.dumps({'observed_at': time.time(), 'event': event})+'\n')
                                stream.flush()
                except (OSError, ValueError):
                    stopped.wait(.2)
    log = (output/'launcher.log').open('w')
    gpu_log = (output/'tegrastats.log').open('w')
    gpu = subprocess.Popen(['tegrastats', '--interval', '1000'], stdout=gpu_log, stderr=subprocess.STDOUT)
    process = subprocess.Popen(command, cwd=root, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    observer = threading.Thread(target=sse_observer, daemon=True)
    observer.start()
    save(output/'pids.json', {'harness': os.getpid(), 'supervisor': process.pid, 'tegrastats': gpu.pid})
    began = time.monotonic()
    owned, jobs, previous = {}, {}, {}
    injection, failure = None, None
    try:
        with (output/'telemetry.jsonl').open('w') as telemetry:
            while process.poll() is None:
                elapsed = time.monotonic()-began
                if stopped.is_set() or elapsed > args.seconds+180:
                    raise RuntimeError('Probe stopped or watchdog expired')
                record = sample(previous)
                previous = record
                changed = True
                while changed:
                    changed = False
                    for pid, state in record['processes'].items():
                        if state['ppid'] == process.pid or str(state['ppid']) in owned:
                            if pid not in owned:
                                changed = True
                            owned[pid] = state
                if args.mode == 'failure' and injection is None and elapsed >= 30:
                    targets = []
                    for pid, state in record['processes'].items():
                        if state['ppid'] == process.pid and any(name in state['command'] for name in ('speech_service.py', 'llama-server')):
                            os.kill(int(pid), signal.SIGTERM)
                            targets.append({'pid': int(pid), 'command': state['command']})
                    injection = {'at': time.time(), 'targets': targets}
                    save(output/'failure-injection.json', injection)
                try:
                    with urllib.request.urlopen('http://127.0.0.1:18080/api/speech/status', timeout=.5) as response:
                        status = json.load(response)
                    for job in status.get('jobs', []):
                        jobs[job['id']] = job
                    record.update(buffered_audio_seconds=status.get('buffered_audio_seconds'),
                                  audio_backlog_seconds=status.get('audio_backlog_seconds'))
                except (OSError, ValueError) as error:
                    record['api_error'] = str(error)
                telemetry.write(json.dumps(record)+'\n')
                telemetry.flush()
                save(output/'progress.json', {'elapsed_seconds': elapsed, 'jobs': len(jobs),
                    'available_GiB': record['available_bytes']/1024**3, 'injected': injection is not None})
                time.sleep(1)
    except Exception as error:
        failure = str(error)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        stopped.set()
        observer.join(4)
        gpu.terminate()
        gpu.wait(timeout=5)
        log.close()
        gpu_log.close()
        for path in (output/'speech/results').glob('*.json'):
            job = json.loads(path.read_text())
            jobs[job['id']] = job
        save(output/'jobs.json', sorted(jobs.values(), key=lambda job: job['created_at']))
        alive = []
        for pid, state in owned.items():
            try:
                current = Path('/proc')/pid/'stat'
                fields = current.read_text().rsplit(')', 1)[1].split()
                if fields[19] == state['start_ticks']:
                    alive.append(int(pid))
                    os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
        save(output/'exit-status.json', {'exit_code': process.returncode, 'failure': failure,
            'elapsed_seconds': time.monotonic()-began, 'owned_processes': owned,
            'remaining_pids_before_cleanup': alive, 'injection': injection})
    print(json.dumps({'output': str(output), 'exit_code': process.returncode, 'failure': failure,
                      'jobs': len(jobs), 'remaining_pids_before_cleanup': alive}))


if __name__ == '__main__':
    main()
