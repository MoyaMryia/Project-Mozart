#!/usr/bin/env python3
"""测量固定输入的识别、可选精修和参考音色预览。"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import numpy as np
import soundfile as sf

from run_efficiency_campaign import free_port, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--recognition-only', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.repo/'tools'))
    from stt_service import build_recognizer
    from asr_checks import refine_with_numeric_check
    from boundary_classifier import BoundaryClassifier
    import sherpa_onnx as so
    report = dict(clock='同步调用使用 monotonic_ns；服务任务时间戳使用 time.time',
                  records=[], speech_jobs=[], asr_events=[])
    def measure(feature, function, **identity):
        began = time.monotonic_ns()
        result = function()
        report['records'].append(dict(feature=feature, elapsed_ms=(time.monotonic_ns()-began)/1e6, **identity))
        return result
    def save():
        report['summary'] = {key: stats([r['elapsed_ms'] for r in report['records'] if r['feature'] == key])
                             for key in sorted({r['feature'] for r in report['records']})}
        (args.output/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    audio, rate = sf.read(args.input, dtype='float32')
    assert rate == 16000 and audio.ndim == 1
    report['input'] = dict(path=str(args.input), sha256=hashlib.sha256(args.input.read_bytes()).hexdigest(),
                           samples=len(audio), sample_rate=rate)
    home = Path.home()
    recognizer = measure('ASR model constructor', lambda: build_recognizer(argparse.Namespace(
        model=home/'models/sherpa-onnx/zipformer-zh-14M', threads=2)))
    frames = np.pad(audio, (0, (-len(audio)) % 320+320*100)).reshape(-1, 320)
    for repeat in range(3):
        stream = recognizer.create_stream()
        began = time.monotonic()
        previous, first, finals = '', None, []
        for index, frame in enumerate(frames):
            remaining = began+index*.02-time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            def decode():
                stream.accept_waveform(16000, frame)
                while recognizer.is_ready(stream):
                    measure('ASR active decode call', lambda: recognizer.decode_stream(stream), repeat=repeat, frame=index)
                return recognizer.get_result(stream)
            text = measure('ASR accept and available decode per 20ms frame', decode, repeat=repeat, frame=index)
            now = time.monotonic()
            if text.strip() and first is None:
                first = (now-began)*1000
            if text != previous:
                report['asr_events'].append(dict(repeat=repeat, frame=index, text=text, at_ms=(now-began)*1000))
                previous = text
            if recognizer.is_endpoint(stream):
                if text.strip():
                    finals.append(dict(text=text, at_ms=(now-began)*1000, frame=index))
                recognizer.reset(stream)
                previous = ''
        report.setdefault('asr_runs', []).append(dict(repeat=repeat, first_partial_ms=first, finals=finals,
            last_input_sample_ms=len(audio)/rate*1000,
            input_end_to_last_final_ms=finals[-1]['at_ms']-len(audio)/rate*1000 if finals else None))
        save()
    del recognizer
    model = home/'models/sherpa-onnx/clone-test-downloads/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'
    refiner = measure('SenseVoice model constructor', lambda: so.OfflineRecognizer.from_sense_voice(
        model=str(model/'model.int8.onnx'), tokens=str(model/'tokens.txt'), num_threads=2, language='zh', use_itn=True))
    for repeat in range(5):
        text, audit = measure('SenseVoice complete 8.399s utterance',
            lambda: refine_with_numeric_check(refiner, audio, '你好这是实时语音测试今天的天气很好'), repeat=repeat)
        report.setdefault('refinement', []).append(dict(text=text, audit=audit))
    del refiner
    boundary = measure('boundary model constructor', lambda: BoundaryClassifier(
        home/'mozart-efficiency-20261014/reference/nnsplit-zh.onnx'))
    for repeat in range(20):
        measure('boundary classifier fixed 30 chars', lambda: boundary.predict('你好这是实时语音测试今天的天气很好我们继续测量每个功能的延迟', ''), repeat=repeat)
    del boundary
    if args.recognition_only:
        if any(run['first_partial_ms'] is None or not run['finals'] for run in report['asr_runs']):
            raise RuntimeError('识别测量没有产生有效字幕')
        save()
        print(json.dumps(report['summary'], indent=2))
        return
    from subtitle_bridge import translate
    translator = home/'mozart-archive/qwen35/llama.cpp/build-cuda-bin/llama-server'
    translation_port = free_port(socket.SOCK_STREAM)
    translation_url = f'http://127.0.0.1:{translation_port}'
    translation_command = [str(translator), '-m',
        str(home/'mozart-archive/qwen35/gguf/qwen35-0.8b-text-Q4_K_M.gguf'),
        '-c', '2048', '-b', '256', '-ub', '128', '--cache-ram', '128', '-ngl', '99',
        '-np', '1', '-t', '2', '-tb', '2', '--host', '127.0.0.1', '--port', str(translation_port), '--jinja']
    report['translation_command'] = translation_command
    translation_env = {**os.environ, 'LD_LIBRARY_PATH': str(translator.parent)+':'+os.environ.get('LD_LIBRARY_PATH', '')}
    with (args.output/'translation.log').open('w') as log:
        began = time.monotonic()
        process = subprocess.Popen(translation_command, env=translation_env, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            while True:
                if process.poll() is not None or time.monotonic()-began > 90:
                    raise RuntimeError('翻译模型启动失败或超时')
                try:
                    with urllib.request.urlopen(translation_url+'/health', timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                time.sleep(.02)
            report['translator_ready_ms'] = (time.monotonic()-began)*1000
            for repeat in range(3):
                for index, source in enumerate(['你好，这是语音延迟测试。', '今天的天气很好，我们下午去公园散步。',
                                               '请在下午三点提醒我，我不需要明天的提醒。']):
                    audit = []
                    def translated():
                        try:
                            return dict(result=translate(translation_url, source, audit=audit), audit=audit)
                        except ValueError as error:
                            return dict(error=str(error), audit=audit)
                    result = measure('isolated translation first request' if repeat == index == 0 else 'isolated translation warm request',
                                     translated, repeat=repeat, text=source)
                    report.setdefault('translations', []).append(dict(source=source, **result))
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            report['translator_exit_code'] = process.returncode
            save()
    port = free_port(socket.SOCK_STREAM)
    env = {**os.environ, 'PYTHONPATH': str(args.runtime)}
    command = [sys.executable, str(args.repo/'tools/speech_service.py'), '--model',
        str(home/'models/sherpa-onnx/clone-test-downloads/sherpa-onnx-pocket-tts-int8-2026-01-26'),
        '--port', str(port), '--data-dir', str(args.output/'speech'), '--preload', '--early-audio',
        '--playback-device', 'null', '--delivery-policy', 'coverage']
    report['speech_command'] = command
    def api(route, body=None, method=None, feature=None):
        req = urllib.request.Request(f'http://127.0.0.1:{port}{route}', method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json'})
        def call():
            with urllib.request.urlopen(req, timeout=10) as response:
                raw = response.read()
            return raw if raw.startswith(b'RIFF') else json.loads(raw)
        return measure(feature, call) if feature else call()
    with (args.output/'speech.log').open('w') as log:
        began = time.monotonic()
        process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            first_http = None
            while True:
                if process.poll() is not None:
                    raise RuntimeError('参考音色服务启动失败')
                if time.monotonic()-began > 90:
                    raise TimeoutError('参考音色服务启动超时')
                try:
                    status = api('/api/speech/status')
                    first_http = first_http or (time.monotonic()-began)*1000
                    if status.get('warmup_error'):
                        raise RuntimeError(status['warmup_error'])
                    if status.get('runtime'):
                        break
                except OSError:
                    pass
                time.sleep(.02)
            report['speech_http_ready_ms'] = first_http
            report['speech_runtime_ready_ms'] = (time.monotonic()-began)*1000
            report['speech_runtime'] = status['runtime']
            voice = api('/api/voices', dict(name='latency reference',
                audio_base64=base64.b64encode(args.reference.read_bytes()).decode(), language='zh'), feature='upload reference voice')
            for route in ['voices', 'speech/status', 'speech/session', 'speech/jobs']:
                for _ in range(20):
                    api('/api/'+route, feature='GET '+route)
            for _ in range(20):
                api('/api/speech/session', dict(enabled=False, playback=False), feature='speech session update')
            for mode in ['preview', 'early playback']:
                for repeat in range(3):
                    began_job = time.monotonic()
                    job = api('/api/speech/jobs', dict(voice_id=voice['id'], language='en',
                        text='Hello. This is a voice latency test. The weather is good today.',
                        live=mode != 'preview', playback=mode != 'preview'), feature='submit '+mode)
                    while (job['status'] not in ['completed', 'failed', 'cancelled', 'expired']
                           or (job['status'] == 'completed' and 'finished_at' not in job)):
                        if time.monotonic()-began_job > 90:
                            raise TimeoutError('参考音色任务超时')
                        time.sleep(.02)
                        job = api('/api/speech/jobs/'+job['id'])
                    report['speech_jobs'].append(dict(mode=mode, repeat=repeat, job=job,
                        submit_to_completed_observed_ms=(time.monotonic()-began_job)*1000))
                    save()
                    if job['status'] != 'completed':
                        raise RuntimeError(f'参考音色任务失败：{job}')
                    wav = api('/api/speech/jobs/'+job['id']+'/result', feature='download '+mode)
                    (args.output/f'{mode.replace(" ", "-")}-{repeat}.wav').write_bytes(wav)
                    for _ in range(10):
                        api('/api/speech/jobs/'+job['id'], feature='GET completed speech job')
            for _ in range(20):
                api('/api/speech/stop', {}, feature='speech stop idle')
            api('/api/voices/'+voice['id'], method='DELETE', feature='delete reference voice')
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            report['speech_exit_code'] = process.returncode
            save()
    print(json.dumps(report['summary'], indent=2))


if __name__ == '__main__':
    main()
