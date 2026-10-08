#!/usr/bin/env python3
"""Start and supervise Mozart's translated reference-speech stack on a Jetson.

An input audio/video file can replace the microphone at normal wall-clock pace.
All child processes belong to this invocation and are stopped together.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import threading

ROOT = Path(__file__).resolve().parents[1]


def assert_ports_available(ports):
    for port, kind in ports:
        with socket.socket(socket.AF_INET, kind) as check:
            if kind == socket.SOCK_STREAM:
                # Ignore TIME_WAIT from our previous run, but listen as well as
                # bind so SO_REUSEADDR cannot hide an existing TCP listener.
                check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            check.bind(('127.0.0.1', port))
            if kind == socket.SOCK_STREAM:
                check.listen(1)


def check_services(processes, available_kib=None, optional_names=(), degraded=None):
    if degraded is None:
        degraded = set()
    for name, process, _ in processes:
        code = process.poll()
        if code is not None:
            if name not in optional_names:
                raise RuntimeError(f'{name} exited ({code})')
            if name not in degraded:
                degraded.add(name)
                print(f'[stack] {name} unavailable ({code}); source captions continue', file=sys.stderr, flush=True)
    if available_kib is None:
        available_kib = next(int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()
                             if line.startswith('MemAvailable:'))
    if available_kib < 768*1024:
        raise RuntimeError('Available memory fell below 768 MiB')


def main():
    home = Path.home()
    assets = home/'models/sherpa-onnx'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend-config', type=Path, required=True)
    parser.add_argument('--model', type=Path, default=assets/'clone-test-downloads/sherpa-onnx-pocket-tts-int8-2026-01-26')
    parser.add_argument('--stt-model', type=Path, default=assets/'zipformer-zh-14M')
    parser.add_argument('--final-model', type=Path, default=None, help='Optional SenseVoice final-utterance model')
    parser.add_argument('--llama-server', type=Path, default=home/'mozart-archive/qwen35/llama.cpp/build-cuda-bin/llama-server')
    parser.add_argument('--llama-model', type=Path, default=home/'mozart-archive/qwen35/gguf/qwen35-0.8b-text-Q4_K_M.gguf')
    parser.add_argument('--translation-batch-size', type=int, default=256)
    parser.add_argument('--translation-ubatch-size', type=int, default=128)
    parser.add_argument('--translation-cache-mib', type=int, default=128,
                        help='Bound cached prompt states; runtime default can exceed Jetson RAM')
    parser.add_argument('--reference', type=Path, help='Optional WAV to register and select at startup')
    parser.add_argument('--reference-name', default='Reference voice')
    parser.add_argument('--input', type=Path, help='Audio/video file replacing microphone')
    parser.add_argument('--keep-open', action='store_true',
                        help='Keep the API and voice previews available after file replay finishes')
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--seconds', type=float, default=60, help='File replay length; microphone runs until stopped')
    parser.add_argument('--mic-device', default='plughw:CARD=Device,DEV=0')
    parser.add_argument('--playback-device', default='plughw:CARD=Device,DEV=0')
    parser.add_argument('--data-dir', type=Path, default=home/'.local/share/mozart/speech')
    parser.add_argument('--run-dir', type=Path, default=Path('/tmp/mozart-translated-speech'))
    parser.add_argument('--no-playback', action='store_true', help='Generate downloadable speech without ALSA playback')
    parser.add_argument('--no-rnnoise', action='store_true')
    parser.add_argument('--archive-utterances',action='store_true',help='Save processed source utterances for diagnosis')
    args = parser.parse_args()
    if args.keep_open and not args.input:
        parser.error('--keep-open requires file input; microphone capture already runs until stopped')
    if args.seconds <= 0 or args.start < 0:
        parser.error('seconds must be positive and start nonnegative')
    if not (0 < args.translation_ubatch_size <= args.translation_batch_size <= 2048) or args.translation_cache_mib < 0:
        parser.error('translation batch sizes must satisfy 0 < ubatch <= batch <= 2048; cache must be nonnegative')
    args.run_dir.mkdir(parents=True, exist_ok=True)
    owned = []
    stopped = False
    speech_startup = None
    degraded = set()
    optional_names = {'speech', 'translation'}
    def stop(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    def launch(name, command, env=None):
        log = (args.run_dir/f'{name}.log').open('w')
        try:
            process = subprocess.Popen([str(x) for x in command], cwd=ROOT, env=env,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        except Exception:
            log.close()
            raise
        owned.append((name, process, log))
        return process
    def optional_failure(name, error):
        degraded.add(name)
        print(f'[stack] {name} unavailable: {error}; source captions continue', file=sys.stderr, flush=True)
    def launch_optional(name, command, env=None):
        try:
            return launch(name, command, env)
        except OSError as error:
            optional_failure(name, error)
            return None
    def finish(process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
    def request(path, body=None):
        req = urllib.request.Request('http://127.0.0.1:18080'+path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)
    def ready(url, process):
        end = time.monotonic()+90
        while time.monotonic() < end and not stopped:
            if process.poll() is not None:
                raise RuntimeError(f'Service exited; see {args.run_dir}')
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200: return
            except OSError:
                pass
            time.sleep(.2)
        raise RuntimeError(f'Readiness timeout: {url}')
    def prepare_speech(process):
        try:
            ready('http://127.0.0.1:18080/api/speech/status', process)
            deadline = time.monotonic()+90
            while time.monotonic() < deadline and not stopped:
                status = request('/api/speech/status')
                if status.get('warmup_error'):
                    raise RuntimeError(status['warmup_error'])
                if status.get('runtime'):
                    break
                if process.poll() is not None:
                    raise RuntimeError('Speech service exited during warmup')
                time.sleep(.2)
            else:
                if stopped:
                    return
                raise RuntimeError('Speech warmup readiness timeout')
            if args.reference and not stopped:
                voice = request('/api/voices', {'name': args.reference_name,
                    'audio_base64': base64.b64encode(args.reference.read_bytes()).decode()})
                request('/api/speech/session', {'enabled': True, 'voice_id': voice['id'],
                    'language': 'en', 'playback': not args.no_playback})
        except Exception as error:
            if not stopped:
                optional_failure('speech', error)
    # Refuse to share unrelated service ports; this invocation owns cleanup.
    assert_ports_available([(18080,socket.SOCK_STREAM),(18081,socket.SOCK_STREAM),
                            (18200,socket.SOCK_STREAM),(18100,socket.SOCK_DGRAM)])
    try:
        env = os.environ.copy()
        env['MOZART_SUBTITLES_JSONL'] = str(args.run_dir/'subtitles.jsonl')
        backend = launch('backend', [ROOT/'build-gpu/state/mozart_stated', args.backend_config.resolve()], env)
        ready('http://127.0.0.1:18080/api/status', backend)
        speech = launch_optional('speech', [sys.executable, ROOT/'tools/speech_service.py', '--model', args.model,
            '--preload','--data-dir', args.data_dir, '--playback-device', args.playback_device])
        if speech is not None:
            speech_startup = threading.Thread(target=prepare_speech, args=(speech,), daemon=True)
            speech_startup.start()
        env = os.environ.copy()
        env['LD_LIBRARY_PATH'] = str(args.llama_server.parent)+':'+env.get('LD_LIBRARY_PATH','')
        launch_optional('translation', [args.llama_server, '-m', args.llama_model, '-c', '2048',
            '-b',args.translation_batch_size,'-ub',args.translation_ubatch_size,'--cache-ram',args.translation_cache_mib,
            '-ngl','99','-np','1','-t','2','-tb','2','--host','127.0.0.1','--port','18200','--jinja'], env)
        bridge_command = [sys.executable, ROOT/'tools/subtitle_bridge.py',
            '--stt-model', args.stt_model, '--jsonl', args.run_dir/'subtitles.jsonl', '--speak']
        if args.final_model:
            bridge_command += ['--final-model', args.final_model]
        if args.archive_utterances:
            bridge_command += ['--utterance-dir',args.run_dir/'utterances']
        bridge = launch('captions', bridge_command)
        for _ in range(300):
            if stopped or bridge.poll() is not None:
                raise RuntimeError('ASR startup interrupted')
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as check:
                try: check.bind(('127.0.0.1',18100))
                except OSError: break
            time.sleep(.1)
        else:
            raise RuntimeError('ASR readiness timeout')
        with tempfile.TemporaryDirectory(prefix='mozart-replay-') as temporary:
            pre_args = [ROOT/'build-gpu/preprocessor/mozart-pre','-a','127.0.0.1','-p','18100']
            if args.input:
                wav = Path(temporary)/'microphone.wav'
                subprocess.run(['ffmpeg','-v','error','-y','-ss',str(args.start),'-i',str(args.input.resolve()),
                    '-t',str(args.seconds),'-ar','48000','-ac','2','-c:a','pcm_s16le',str(wav)], check=True, timeout=120)
                pre_args += ['-i',wav,'--pace','-n',str(round(args.seconds/.02))]
            else:
                pre_args += ['-d',args.mic_device]
            if args.no_rnnoise: pre_args += ['--no-rnnoise']
            pre = launch('microphone', pre_args)
            print(f'[stack] running; API port 18080, logs {args.run_dir}', flush=True)
            while not stopped and pre.poll() is None:
                check_services([entry for entry in owned if entry[1] is not pre],
                               optional_names=optional_names, degraded=degraded)
                time.sleep(.2)
            if pre.poll() not in [None, 0]:
                raise RuntimeError(f'Preprocessor exited ({pre.returncode})')
            finish(pre)
            # Terminate only the bridge first. It drains its STT child's final output.
            bridge.terminate()
            bridge.wait(timeout=30)
            if not stopped:
                end = time.monotonic()+120
                while time.monotonic() < end:
                    remaining = [entry for entry in owned if entry[0] in ('backend', 'speech', 'translation')]
                    check_services(remaining, optional_names=optional_names, degraded=degraded)
                    try:
                        status = request('/api/speech/status') if speech is not None else {'available': False, 'jobs': []}
                    except (OSError, ValueError) as error:
                        optional_failure('speech', error)
                        status = {'available': False, 'error': str(error), 'jobs': []}
                        break
                    if all(j['status'] in ['completed','failed','cancelled','expired'] for j in status['jobs']): break
                    time.sleep(.2)
                (args.run_dir/'speech-results.json').write_text(json.dumps(status, ensure_ascii=False, indent=2))
            if args.keep_open and not stopped:
                print('[stack] mock input finished; API and voice previews remain available until stopped', flush=True)
                remaining = [entry for entry in owned if entry[0] in ('backend', 'speech', 'translation')]
                while not stopped:
                    check_services(remaining, optional_names=optional_names, degraded=degraded)
                    time.sleep(.2)
    finally:
        stopped = True
        for _, process, log in reversed(owned):
            finish(process)
            log.close()
        if speech_startup:
            speech_startup.join(timeout=6)


if __name__ == '__main__':
    main()
