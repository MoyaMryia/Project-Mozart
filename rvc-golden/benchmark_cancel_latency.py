#!/usr/bin/env python3
"""测量任务执行期间的取消、模式切换和语音代理延迟。"""
import argparse
import base64
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request

from run_efficiency_campaign import free_port, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    work, home = args.work, Path.home()
    output = work/'feature-latency-cancel'
    output.mkdir(exist_ok=False)
    native_port, speech_port = free_port(socket.SOCK_STREAM), free_port(socket.SOCK_STREAM)
    report, processes, logs = dict(records=[]), [], []
    def api(port, route, body=None, method=None, feature=None, content_type='application/json'):
        request = urllib.request.Request(f'http://127.0.0.1:{port}{route}', method=method,
            data=body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': content_type})
        began = time.monotonic()
        with urllib.request.urlopen(request, timeout=15) as response:
            value = json.load(response)
        if feature:
            report['records'].append(dict(feature=feature, elapsed_ms=(time.monotonic()-began)*1000))
        return value
    def until(function, timeout=90):
        began = time.monotonic()
        while True:
            if any(process.poll() is not None for process in processes):
                raise RuntimeError('测试服务退出')
            try:
                value = function()
                if value:
                    return value
            except OSError:
                pass
            if time.monotonic()-began > timeout:
                raise TimeoutError('测试状态等待超时')
            time.sleep(.02)
    def launch(name, command, environment):
        log = (output/(name+'.log')).open('w')
        logs.append(log)
        process = subprocess.Popen(command, env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        processes.append(process)
    config = json.loads((work/'feature-latency-native-gpu/backend.json').read_text())
    config['network']['control']['port'] = native_port
    config['network']['audio']['port'] = free_port(socket.SOCK_DGRAM)
    config['storage']['temp_dir'] = str(output/'temp')
    config['storage']['presets_path'] = str(output/'presets.json')
    (output/'backend.json').write_text(json.dumps(config, indent=2)+'\n')
    try:
        launch('backend', [str(work/'build-candidate/state/mozart_stated'), str(output/'backend.json')],
            {**os.environ, 'MOZART_RVC_PROFILE': '1', 'MOZART_SPEECH_PORT': str(speech_port)})
        until(lambda: api(native_port, '/api/status'))
        api(native_port, '/api/mode/switch', dict(mode='file_rvc', model_id='quality'))
        boundary = 'MozartCancelLatencyBoundary'
        raw = (work/'reference/input/qiqi-espeak.wav').read_bytes()
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model_id"\r\n\r\nquality\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="audio_file"; filename="input.wav"\r\n'
                'Content-Type: audio/wav\r\n\r\n').encode()+raw+f'\r\n--{boundary}--\r\n'.encode()
        key = api(native_port, '/api/file/convert', body, content_type='multipart/form-data; boundary='+boundary)['job_id']
        until(lambda: '[profile][generator] frames=398' in (output/'backend.log').read_text())
        began = time.monotonic()
        report['cancel_response'] = api(native_port, '/api/file/cancel?job_id='+key, method='DELETE', feature='active file cancel acceptance')
        report['pending_response'] = api(native_port, '/api/mode/switch', dict(mode='idle'), feature='pending mode switch acceptance')
        def cancelled():
            job = api(native_port, '/api/file/status?job_id='+key)
            return job if job['status'] == 'cancelled' else None
        report['cancelled_file'] = until(cancelled)
        report['file_cancel_to_terminal_ms'] = (time.monotonic()-began)*1000
        until(lambda: api(native_port, '/api/status')['mode'] == 'idle')
        report['file_cancel_to_idle_ms'] = (time.monotonic()-began)*1000
        launch('speech', [sys.executable, str(args.repo/'tools/speech_service.py'), '--model',
            str(home/'models/sherpa-onnx/clone-test-downloads/sherpa-onnx-pocket-tts-int8-2026-01-26'),
            '--port', str(speech_port), '--data-dir', str(output/'speech'), '--preload', '--early-audio',
            '--playback-device', 'null', '--delivery-policy', 'coverage'],
            {**os.environ, 'PYTHONPATH': str(home/'mozart-realtime-patches-20261008/early-audio-runtime-20261009/python')})
        until(lambda: api(speech_port, '/api/speech/status').get('runtime'))
        for _ in range(20):
            api(speech_port, '/api/speech/status', feature='direct speech status')
            api(native_port, '/api/speech/status', feature='proxied speech status')
        reference = home/'Mozart/rvc-golden/clone-tts/qiqi-20261004/reference-qiqi-natural-source.wav'
        voice = api(native_port, '/api/voices', dict(name='cancel latency', language='zh',
            audio_base64=base64.b64encode(reference.read_bytes()).decode()))
        body = dict(voice_id=voice['id'], language='en', text='This is a test of speech cancellation. Please stop the current task before it finishes.', playback=False)
        active = api(native_port, '/api/speech/jobs', body)
        until(lambda: api(native_port, '/api/speech/jobs/'+active['id']).get('started_at'))
        time.sleep(.2)
        queued = api(native_port, '/api/speech/jobs', body)
        report['speech_queued_cancel'] = api(native_port, '/api/speech/jobs/'+queued['id'], method='DELETE', feature='queued speech cancel')
        report['speech_active_cancel'] = api(native_port, '/api/speech/jobs/'+active['id'], method='DELETE', feature='active speech cancel')
        api(native_port, '/api/speech/session', dict(enabled=True, voice_id=voice['id'], language='en', playback=True))
        playing = api(native_port, '/api/speech/events', dict(text=body['text'], utterance_id='cancel-playback'))
        until(lambda: api(native_port, '/api/speech/jobs/'+playing['id']).get('playback_started_at'))
        report['speech_stop'] = api(native_port, '/api/speech/stop', {}, feature='active speech stop')
        report['speech_stopped_job'] = api(native_port, '/api/speech/jobs/'+playing['id'])
        if any(job['status'] != 'cancelled' for job in [report['speech_queued_cancel'], report['speech_active_cancel'], report['speech_stopped_job']]):
            raise RuntimeError('取消后的任务状态不正确')
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        for log in logs:
            log.close()
        report['summary'] = {key: stats([r['elapsed_ms'] for r in report['records'] if r['feature'] == key])
                             for key in sorted({r['feature'] for r in report['records']})}
        (output/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report['summary'], indent=2))


if __name__ == '__main__':
    main()
