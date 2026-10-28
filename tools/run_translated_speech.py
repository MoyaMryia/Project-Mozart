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
import uuid
import math
from tempo_control import DEFAULT_TEMPO_MAX, validate_tempo_limits

ROOT = Path(__file__).resolve().parents[1]
# 语音服务端口只用这一处定义；字幕桥按同一地址提交播报。
SPEECH_PORT = 18081
STARTUP_TIMEOUT_SECONDS = 90
AUDIO_TIMEOUT_SECONDS = 30
POLL_SECONDS = .2


class ServiceUnavailable(RuntimeError):
    """外部服务未就绪或返回无效响应。"""


class StopRequested(Exception):
    """停止等待并回收当前启动器拥有的服务。"""


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


def check_services(processes, available_kib=None, optional_names=(), degraded=None, on_degraded=None):
    if degraded is None:
        degraded = set()
    for name, process, _ in processes:
        code = process.poll()
        if code is not None:
            if name not in optional_names:
                raise RuntimeError(f'{name} exited ({code})')
            if name not in degraded:
                degraded.add(name)
                if on_degraded:
                    on_degraded(name, f'exited ({code})')
                print(f'[stack] {name} unavailable ({code}); source captions continue', file=sys.stderr, flush=True)
    if available_kib is None:
        available_kib = next(int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()
                             if line.startswith('MemAvailable:'))
    if available_kib < 768*1024:
        raise RuntimeError('Available memory fell below 768 MiB')


def unfinished_speech(status):
    if 'unfinished_jobs' in status:
        return status['unfinished_jobs']
    return sum(j['status'] not in ['completed', 'failed', 'cancelled', 'expired'] for j in status.get('jobs', []))


def main():
    home = Path.home()
    assets = home/'models/sherpa-onnx'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend-config', type=Path, required=True)
    parser.add_argument('--api-port', type=int, default=18080, help='后端 HTTP 端口，与 backend-config 保持一致')
    parser.add_argument('--mode', choices=['full', 'captions'], default='full')
    parser.add_argument('--allow-degraded', action='store_true',
                        help='Allow source captions before optional translation and speech become ready')
    parser.add_argument('--model', type=Path, default=assets/'clone-test-downloads/sherpa-onnx-pocket-tts-int8-2026-01-26')
    parser.add_argument('--tts-provider', choices=['cpu', 'cuda'], default='cpu')
    parser.add_argument('--tts-precision', choices=['int8', 'float32'], default='int8')
    parser.add_argument('--tts-pythonpath', type=Path, help='Independent sherpa package directory for the speech service')
    parser.add_argument('--tts-threads', type=int, default=2)
    parser.add_argument('--early-audio', action='store_true')
    parser.add_argument('--tts-audio-chunk-frames', type=int, choices=[3, 5, 10, 15], default=3)
    parser.add_argument('--stable-clauses', action='store_true')
    parser.add_argument('--boundary-model', type=Path, help='Optional local Chinese NNSplit ONNX model')
    parser.add_argument('--boundary-threshold', type=float, default=.5)
    parser.add_argument('--clause-max-chars', type=int, default=24)
    parser.add_argument('--delivery-policy', choices=['coverage', 'realtime'], default='coverage')
    parser.add_argument('--tts-provider-config', type=Path)
    parser.add_argument('--stt-model', type=Path, default=assets/'zipformer-zh-14M')
    parser.add_argument('--final-model', type=Path, default=None, help='Optional SenseVoice final-utterance model')
    parser.add_argument('--glossary', type=Path, help='JSON 术语表；同时供控制台编辑')
    parser.add_argument('--corrections', type=Path, help='JSON 同音纠错表')
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
    parser.add_argument('--mic-device',
                        default=os.environ.get('MOZART_MIC_DEVICE', 'plughw:CARD=Device,DEV=0'))
    parser.add_argument('--playback-device',
                        default=os.environ.get('MOZART_PLAYBACK_DEVICE', 'plughw:CARD=Device,DEV=0'))
    parser.add_argument('--playback-tempo', choices=['normal', 'adaptive', 'flexible'], default='flexible')
    parser.add_argument('--duration-profile', type=Path)
    parser.add_argument('--playback-tempo-max', type=float, default=DEFAULT_TEMPO_MAX)
    parser.add_argument('--playback-target-delay', type=float, default=1.5)
    parser.add_argument('--playback-recovery-seconds', type=float, default=30.)
    parser.add_argument('--capture-playback', action='store_true')
    parser.add_argument('--data-dir', type=Path, default=home/'.local/share/mozart/speech')
    parser.add_argument('--run-dir', type=Path, default=Path('/tmp/mozart-translated-speech'))
    parser.add_argument('--no-playback', action='store_true', help='Generate downloadable speech without ALSA playback')
    parser.add_argument('--external-capture', action='store_true',
                        help='外部进程已提供 MZRT 音频到 18100，本栈不启动采集预处理器')
    parser.add_argument('--no-denoise', dest='no_denoise', action='store_true')
    parser.add_argument('--archive-utterances',action='store_true',help='Save processed source utterances for diagnosis')
    args = parser.parse_args()
    if not 1 <= args.api_port <= 65535:
        parser.error('api-port must be from 1 to 65535')
    api_base = f'http://127.0.0.1:{args.api_port}'
    try:
        validate_tempo_limits(args.playback_tempo_max, args.playback_target_delay, args.playback_recovery_seconds)
    except ValueError as error:
        parser.error(str(error))
    if args.boundary_model:
        args.stable_clauses = True
    if args.tts_threads < 1:
        parser.error('tts-threads must be positive')
    if args.stable_clauses and args.delivery_policy != 'coverage':
        parser.error('stable-clauses requires the coverage delivery policy')
    if not 0 < args.boundary_threshold < 1 or not 12 <= args.clause_max_chars <= 64:
        parser.error('boundary-threshold must be between 0 and 1; clause-max-chars must be from 12 to 64')
    if args.keep_open and not args.input:
        parser.error('--keep-open requires file input; microphone capture already runs until stopped')
    if args.external_capture and args.input:
        parser.error('--external-capture requires microphone capture, not file replay')
    if not math.isfinite(args.seconds) or not math.isfinite(args.start) or args.seconds <= 0 or args.start < 0:
        parser.error('seconds must be positive and start nonnegative')
    if args.input and args.seconds < .02:
        parser.error('File replay must contain at least one 20 ms frame')
    if not (0 < args.translation_ubatch_size <= args.translation_batch_size <= 2048) or args.translation_cache_mib < 0:
        parser.error('translation batch sizes must satisfy 0 < ubatch <= batch <= 2048; cache must be nonnegative')
    reference_audio = None
    if args.reference:
        try:
            reference_audio = base64.b64encode(args.reference.read_bytes()).decode()
        except OSError as error:
            parser.error(f'Reference audio: {error}')
    if args.mode == 'captions' and args.reference:
        parser.error('--reference requires full mode')
    args.run_dir.mkdir(parents=True, exist_ok=True)
    owned = []
    finished = set()
    stopped = False
    degraded = set()
    optional_names = {'speech', 'translation'} if args.allow_degraded else set()
    state = {'version': 1, 'instance_id': uuid.uuid4().hex, 'supervisor_pid': os.getpid(),
             'requirements': {'language': 'en', 'playback': not args.no_playback,
                              'voice_required': args.mode == 'full'},
             'mode': args.mode, 'allow_degraded': args.allow_degraded,
             'state': 'starting', 'phase': 'initializing', 'started_at': time.time(),
             'readiness': {'translation': False, 'speech_runtime': False, 'voice_session': False},
             'degraded': {}, 'children': {}, 'capture_started_at': None}
    status_path = args.run_dir/'stack-status.json'

    def save(**changes):
        state.update(changes)
        state['updated_at'] = time.time()
        temporary = status_path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        temporary.replace(status_path)

    def stop(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def record_degraded(name, error):
        reason = str(error)
        if state['degraded'].get(name) != reason:
            save(degraded={**state['degraded'], name: reason})
            print(f'[stack] {name} unavailable: {reason}; source captions continue', file=sys.stderr, flush=True)

    def launch(name, command, env=None):
        with (args.run_dir/f'{name}.log').open('w') as log:
            try:
                process = subprocess.Popen([str(x) for x in command], cwd=ROOT, env=env,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as error:
                raise ServiceUnavailable(f'{name} launch failed: {error}') from error
        owned.append((name, process, log))
        save(children={**state['children'], name: {'pid': process.pid, 'exit_code': None,
                                                 'termination_requested': False, 'forced': False}})
        return process

    def launch_optional(name, command, env=None):
        try:
            return launch(name, command, env)
        except ServiceUnavailable as error:
            if not args.allow_degraded:
                raise
            record_degraded(name, error)
            return None

    def finish(name, process):
        if name in finished:
            return
        child = state['children'][name]
        if process.poll() is None:
            child['termination_requested'] = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                child['forced'] = True
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        # 主进程退出后，回收同一进程组内未响应 SIGTERM 的后代。
        try:
            os.killpg(process.pid, signal.SIGKILL)
            child['forced'] = True
        except ProcessLookupError:
            pass
        child['exit_code'] = process.poll()
        finished.add(name)

    def service_request(url, body=None, timeout=1):
        request = urllib.request.Request(url,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.load(response)
        except (OSError, json.JSONDecodeError) as error:
            raise ServiceUnavailable(f'{url}: {error}') from error
        if not isinstance(result, dict):
            raise ServiceUnavailable(f'{url}: expected a JSON object')
        return result

    def request(path, body=None):
        return service_request(api_base+path, body, timeout=5)

    def monitor(entries):
        children = {name: {**state['children'][name], 'exit_code': process.poll()}
                    for name, process, _ in owned}
        readiness = dict(state['readiness'])
        if children.get('translation', {}).get('exit_code') is not None:
            readiness['translation'] = False
        if children.get('speech', {}).get('exit_code') is not None:
            readiness.update(speech_runtime=False, voice_session=False)
        if children != state['children'] or readiness != state['readiness']:
            save(children=children, readiness=readiness)
        check_services(entries, optional_names=optional_names, degraded=degraded,
                       on_degraded=record_degraded)

    def ready(url, process):
        deadline = time.monotonic()+STARTUP_TIMEOUT_SECONDS
        while not stopped:
            if process.poll() is not None:
                raise ServiceUnavailable(f'Service exited; see {args.run_dir}')
            try:
                service_request(url)
                return
            except ServiceUnavailable:
                if time.monotonic() >= deadline:
                    raise ServiceUnavailable(f'Readiness timeout: {url}')
            time.sleep(POLL_SECONDS)
        raise StopRequested()

    startup_deadline = None
    reference_voice = None
    reference_session_started = False
    speech = translation = None

    def mark_ready(name, value):
        if state['readiness'][name] != value:
            save(readiness={**state['readiness'], name: value})

    def startup_failure(name, error):
        if not args.allow_degraded:
            raise error
        record_degraded(name, error)

    def poll_startup():
        nonlocal reference_voice, reference_session_started
        if stopped:
            raise StopRequested()
        if translation is not None and translation.poll() is None:
            had_translation = state['readiness']['translation']
            try:
                health = service_request('http://127.0.0.1:18200/health')
                if health.get('status') != 'ok':
                    raise ServiceUnavailable('Translation model is not ready')
                mark_ready('translation', True)
                if 'translation' in state['degraded']:
                    save(degraded={key: value for key, value in state['degraded'].items() if key != 'translation'})
            except ServiceUnavailable as error:
                mark_ready('translation', False)
                if had_translation or time.monotonic() >= startup_deadline:
                    startup_failure('translation', error)
        if speech is not None and speech.poll() is None:
            had_runtime = state['readiness']['speech_runtime']
            try:
                status = request('/api/speech/status')
            except ServiceUnavailable as error:
                mark_ready('speech_runtime', False)
                mark_ready('voice_session', False)
                if had_runtime or time.monotonic() >= startup_deadline:
                    startup_failure('speech', error)
                return
            failure = status.get('worker_error') or status.get('warmup_error')
            if failure:
                mark_ready('speech_runtime', False)
                mark_ready('voice_session', False)
                startup_failure('speech', ServiceUnavailable(str(failure)))
                return
            if status.get('runtime_ready') is not True:
                mark_ready('speech_runtime', False)
                mark_ready('voice_session', False)
                if had_runtime or status.get('runtime_error') or time.monotonic() >= startup_deadline:
                    reason = status.get('runtime_error') or ('Speech runtime became unavailable'
                        if had_runtime else 'Speech warmup readiness timeout')
                    startup_failure('speech', ServiceUnavailable(reason))
                return
            runtime = status.get('runtime')
            if not isinstance(runtime, dict) or not runtime:
                mark_ready('speech_runtime', False)
                mark_ready('voice_session', False)
                startup_failure('speech', ServiceUnavailable('Ready speech runtime must be a nonempty JSON object'))
                return
            mark_ready('speech_runtime', True)
            activating_reference = False
            try:
                if reference_audio is not None and reference_voice is None:
                    voice = request('/api/voices', {'name': args.reference_name, 'audio_base64': reference_audio})
                    if not isinstance(voice.get('id'), str) or not voice['id']:
                        raise ServiceUnavailable('Reference registration did not return a voice ID')
                    reference_voice = voice['id']
                    save(reference_voice_id=reference_voice)
                if reference_voice is not None and not reference_session_started:
                    activating_reference = True
                    request('/api/speech/session', {'enabled': True, 'voice_id': reference_voice,
                            'language': 'en', 'playback': not args.no_playback})
                    reference_session_started = True
                    activating_reference = False
                    if 'startup_wait' in state:
                        save(startup_wait=None)
                session = request('/api/speech/session')
            except ServiceUnavailable as error:
                mark_ready('voice_session', False)
                if activating_reference and time.monotonic() < startup_deadline:
                    save(startup_wait={'service': 'speech', 'reason': str(error)})
                    return
                startup_failure('speech', error)
                return
            selected = (session.get('enabled') is True and isinstance(session.get('voice_id'), str)
                        and bool(session['voice_id']) and session.get('language') == 'en'
                        and session.get('playback') is (not args.no_playback)
                        and (reference_voice is None or session['voice_id'] == reference_voice))
            mark_ready('voice_session', selected)
            if not selected and state['capture_started_at'] is not None:
                startup_failure('speech', ServiceUnavailable('Speech session became unavailable or playback configuration changed'))
            if selected and 'speech' in state['degraded']:
                save(degraded={key: value for key, value in state['degraded'].items() if key != 'speech'})
        if (state['readiness']['translation'] and (args.mode == 'captions' or
                (state['readiness']['speech_runtime'] and state['readiness']['voice_session']))
                and 'startup' in state['degraded']):
            save(degraded={key: value for key, value in state['degraded'].items() if key != 'startup'})

    def wait_startup():
        while True:
            monitor(owned)
            poll_startup()
            readiness = state['readiness']
            if readiness['translation'] and (args.mode == 'captions' or
                    (readiness['speech_runtime'] and readiness['voice_session'])):
                return
            phase = ('waiting_for_voice' if readiness['translation'] and readiness['speech_runtime']
                     else 'preparing_services')
            if state['phase'] != phase:
                save(phase=phase)
            time.sleep(POLL_SECONDS)

    try:
        save()
        assert_ports_available([(args.api_port, socket.SOCK_STREAM), (SPEECH_PORT, socket.SOCK_STREAM),
                                (18200, socket.SOCK_STREAM), (18100, socket.SOCK_DGRAM)])
        env = os.environ.copy()
        env['MOZART_SUBTITLES_JSONL'] = str(args.run_dir/'subtitles.jsonl')
        audio_status_path = args.run_dir/'audio-status.json'
        env['MOZART_STT_STATUS_JSON'] = str(audio_status_path.resolve())
        env['MOZART_STACK_STATUS_JSON'] = str(status_path.resolve())
        env['MOZART_STACK_INSTANCE_ID'] = state['instance_id']
        # 术语表与纠错表是外部输入，子进程各自读取同一份文件。
        if args.glossary:
            env['MOZART_GLOSSARY'] = str(args.glossary.resolve())
        if args.corrections:
            env['MOZART_CORRECTIONS'] = str(args.corrections.resolve())
        backend = launch('backend', [ROOT/'build-gpu/state/mozart_stated', args.backend_config.resolve()], env)
        ready(api_base+'/api/status', backend)
        speech_command = [sys.executable, ROOT/'tools/speech_service.py', '--model', args.model,
            '--port', str(SPEECH_PORT),
            '--preload','--data-dir', args.data_dir, '--playback-device', args.playback_device,
            '--provider', args.tts_provider, '--precision', args.tts_precision,
            '--threads', args.tts_threads, '--delivery-policy', args.delivery_policy]
        speech_command += ['--playback-tempo', args.playback_tempo]
        speech_command += ['--playback-tempo-max', str(args.playback_tempo_max),
            '--playback-target-delay', str(args.playback_target_delay),
            '--playback-recovery-seconds', str(args.playback_recovery_seconds)]
        if args.duration_profile:
            speech_command += ['--duration-profile', args.duration_profile]
        if args.capture_playback:
            speech_command += ['--capture-playback']
        if args.tts_provider_config:
            speech_command += ['--provider-config', args.tts_provider_config]
        if args.early_audio:
            speech_command += ['--early-audio', '--audio-chunk-frames', str(args.tts_audio_chunk_frames)]
        speech_environment = os.environ.copy()
        if args.tts_pythonpath:
            speech_environment['PYTHONPATH'] = str(args.tts_pythonpath)+os.pathsep+speech_environment.get('PYTHONPATH', '')
        if args.mode == 'full':
            speech = launch_optional('speech', speech_command, speech_environment)
        # 不要覆盖 env：后面的字幕桥要复用同一份术语表与纠错表路径。
        translation_env = dict(env)
        translation_env['LD_LIBRARY_PATH'] = str(args.llama_server.parent)+':'+translation_env.get('LD_LIBRARY_PATH','')
        translation = launch_optional('translation', [args.llama_server, '-m', args.llama_model, '-c', '2048',
            '-b',args.translation_batch_size,'-ub',args.translation_ubatch_size,'--cache-ram',args.translation_cache_mib,
            '-ngl','99','-np','1','-t','2','-tb','2','--host','127.0.0.1','--port','18200','--jinja'], translation_env)
        startup_deadline = time.monotonic()+STARTUP_TIMEOUT_SECONDS
        if args.allow_degraded:
            record_degraded('startup', 'Source captions may begin before required services are ready')
        else:
            wait_startup()
        bridge_command = [sys.executable, ROOT/'tools/subtitle_bridge.py',
            '--stt-model', args.stt_model, '--jsonl', args.run_dir/'subtitles.jsonl',
            '--delivery-policy', args.delivery_policy, '--queue-dir', args.data_dir/'captions']
        if args.final_model:
            bridge_command += ['--final-model', args.final_model]
        if args.stable_clauses:
            bridge_command += ['--stable-clauses']
        if args.boundary_model:
            bridge_command += ['--boundary-model', args.boundary_model,
                '--boundary-threshold', str(args.boundary_threshold), '--clause-max-chars', str(args.clause_max_chars)]
        if args.archive_utterances:
            bridge_command += ['--utterance-dir',args.run_dir/'utterances']
        if args.mode == 'full':
            bridge_command += ['--speak', '--speech-url', f'http://127.0.0.1:{SPEECH_PORT}']
        bridge = launch('captions', bridge_command, env)
        save(phase='preparing_captions')
        deadline = time.monotonic()+STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if stopped:
                raise StopRequested()
            monitor(owned)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as check:
                try:
                    check.bind(('127.0.0.1', 18100))
                except OSError:
                    break
            time.sleep(POLL_SECONDS)
        else:
            raise ServiceUnavailable('ASR readiness timeout')
        with tempfile.TemporaryDirectory(prefix='mozart-replay-') as temporary:
            # 采集用预处理器属于本栈私有，没有外部控制者。默认端口 19040
            # 会与实时变声链路冲突，这里关闭控制通道。
            pre_args = [ROOT/'build-gpu/preprocessor/mozart-pre','-a','127.0.0.1','-p','18100',
                        '--ctl-port','0']
            if args.input:
                wav = Path(temporary)/'microphone.wav'
                subprocess.run(['ffmpeg','-v','error','-y','-ss',str(args.start),'-i',str(args.input.resolve()),
                    '-t',str(args.seconds),'-ar','48000','-ac','2','-c:a','pcm_s16le',str(wav)], check=True, timeout=120)
                pre_args += ['-i',wav,'--pace','-n',str(round(args.seconds/.02))]
            else:
                pre_args += ['-d',args.mic_device]
            if args.no_denoise: pre_args += ['--no-denoise']
            if stopped:
                raise StopRequested()
            monitor(owned)
            pre = None if args.external_capture else launch('microphone', pre_args)
            if pre is not None and pre.poll() not in (None, 0):
                raise RuntimeError(f'Preprocessor exited ({pre.returncode})')
            if pre is not None and pre.poll() is not None and not args.input and not stopped:
                raise RuntimeError('Microphone exited before a stop request')
            capture_deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
            save(state='waiting' if pre is None else 'running',
                 phase='waiting_for_audio' if pre is None else 'capturing',
                 capture_started_at=None if pre is None else time.time())
            print(f'[stack] API {api_base}, logs {args.run_dir}', flush=True)
            while not stopped and (pre is None or pre.poll() is None):
                monitor([entry for entry in owned if entry[1] is not pre])
                poll_startup()
                if pre is None:
                    try:
                        audio = json.loads(audio_status_path.read_text())
                    except FileNotFoundError:
                        audio = {}
                    if audio.get('instance_id') != state['instance_id']:
                        audio = {'packets': 0, 'rejected': 0, 'last_audio_at': None}
                    save(capture=audio)
                    if audio['packets'] > 0 and time.time() - audio['last_audio_at'] < AUDIO_TIMEOUT_SECONDS:
                        if state['capture_started_at'] is None:
                            save(state='running', phase='capturing', capture_started_at=time.time())
                    elif state['capture_started_at'] is not None or time.monotonic() >= capture_deadline:
                        raise ServiceUnavailable(f'External audio unavailable: {audio}')
                time.sleep(POLL_SECONDS)
            if pre is not None and pre.poll() not in (None, 0):
                raise RuntimeError(f'Preprocessor exited ({pre.returncode})')
            if pre is not None and pre.poll() is not None and not args.input and not stopped:
                raise RuntimeError('Microphone exited before a stop request')
            if pre is not None:
                finish('microphone', pre)
            if stopped:
                raise StopRequested()
            save(state='draining', phase='captions')
            bridge.terminate()
            if args.delivery_policy == 'coverage':
                while bridge.poll() is None and not stopped:
                    monitor([entry for entry in owned if entry[0] in ('backend', 'speech', 'translation')])
                    poll_startup()
                    time.sleep(POLL_SECONDS)
            else:
                bridge.wait(timeout=30)
            if stopped:
                raise StopRequested()
            if bridge.poll() not in (None, 0):
                raise RuntimeError(f'Caption drain failed ({bridge.returncode})')
            if args.mode == 'full':
                save(phase='speech')
                end = None if args.delivery_policy == 'coverage' else time.monotonic()+120
                speech_status = {'available': False, 'jobs': []}
                while not stopped and (end is None or time.monotonic() < end):
                    monitor([entry for entry in owned if entry[0] in ('backend', 'speech', 'translation')])
                    poll_startup()
                    try:
                        speech_status = request('/api/speech/status') if speech is not None else speech_status
                    except ServiceUnavailable as error:
                        startup_failure('speech', error)
                        speech_status = {'available': False, 'error': str(error), 'jobs': []}
                        break
                    if not unfinished_speech(speech_status):
                        break
                    time.sleep(POLL_SECONDS)
                if not stopped and unfinished_speech(speech_status):
                    raise RuntimeError('Speech drain deadline expired')
                (args.run_dir/'speech-results.json').write_text(json.dumps(speech_status, ensure_ascii=False, indent=2))
            if args.keep_open and not stopped:
                save(state='running', phase='preview')
                while not stopped:
                    monitor([entry for entry in owned if entry[0] in ('backend', 'speech', 'translation')])
                    poll_startup()
                    time.sleep(POLL_SECONDS)
    except StopRequested:
        stopped = True
    finally:
        error = sys.exc_info()[1]
        final_state = 'failed' if error else 'stopped' if stopped else 'completed'
        if error:
            state['error'] = {'type': type(error).__name__, 'message': str(error)}
        stopped = True
        cleanup_errors = []
        for name, process, _ in reversed(owned):
            try:
                finish(name, process)
            except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                cleanup_errors.append({'service': name, 'error': str(cleanup_error)})
        if cleanup_errors:
            final_state = 'failed'
            if error is None:
                state['error'] = {'type': 'RuntimeError', 'message': f'Service cleanup failed: {cleanup_errors}'}
        save(state=final_state, phase=final_state, finished_at=time.time(), cleanup_errors=cleanup_errors)
        if cleanup_errors and error is None:
            raise RuntimeError(f'Service cleanup failed: {cleanup_errors}')


if __name__ == '__main__':
    main()
