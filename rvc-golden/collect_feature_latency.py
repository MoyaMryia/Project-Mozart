#!/usr/bin/env python3
"""汇总逐功能延迟，并检查保留的音频输出。"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

import numpy as np
from analyze_feature_latency import stats


def read(path):
    return json.loads(path.read_text())


def pcm(path):
    raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 'f32le', '-'])
    return np.frombuffer(raw, dtype='<f4').astype(np.float64)


def compare(left, right):
    if len(left) != len(right):
        raise ValueError('音频输出长度不同')
    if not np.isfinite(left).all() or not np.isfinite(right).all():
        raise ValueError('音频输出包含非有限值')
    return dict(samples=len(left), exact=bool(np.array_equal(left, right)),
                relative_rmse=float(np.linalg.norm(left-right)/np.linalg.norm(left)),
                cosine=float(np.dot(left, right)/np.linalg.norm(left)/np.linalg.norm(right)),
                max_absolute_error=float(np.max(abs(left-right))))


def native(root):
    result = read(root/'results.json')
    log = (root/'backend.log').read_text()
    profiles = {}
    for label, stage, tail in [('realtime', 'realtime', r'total=([\d.]+)ms'),
                        ('generator_file', 'generator', r'total=([\d.]+)ms'),
                        ('hubert_realtime', 'hubert', r'audio=44800 .*?engine=([\d.]+)ms'),
                        ('hubert_file', 'hubert', r'audio=64000 .*?engine=([\d.]+)ms'),
                        ('f0_file', 'f0', r'audio=166382 .*?total=([\d.]+)ms'),
                        ('f0_realtime', 'f0', r'audio=4960 .*?total=([\d.]+)ms'),
                        ('generator_realtime', 'realtime', r'generator=([\d.]+)ms')]:
        values = [float(value) for value in re.findall(r'\[profile\]\['+stage+r'\][^\n]*?'+tail, log)]
        if values:
            profiles[label] = stats(values)
    for item in result['streams'].values():
        stream = item['status']['stream']
        if any(stream.get(key) for key in ['inference_errors', 'input_overruns', 'output_overruns', 'late_blocks', 'resets']):
            raise ValueError('实时流统计包含错误')
        if stream['output_underruns'] != stream['startup_output_underruns']:
            raise ValueError('实时流发生启动后欠载')
    audio = [pcm(path) for path in sorted(root.glob('file-*.wav'))]
    if not audio or not all(np.array_equal(audio[0], value) for value in audio):
        raise ValueError('同配置文件转换重复输出不同')
    return dict(startup_ms=result['startup_ms'], api=result['summary'], profiles=profiles,
        streams={key: dict(roundtrip=value['roundtrip'], first_nonzero_frame_ms=value['first_nonzero_frame_ms'],
                          stream=value['status']['stream']) for key, value in result['streams'].items()},
        file_first_ms=result['files'][0]['upload_to_completed_observed_ms'],
        file_warm=stats([item['upload_to_completed_observed_ms'] for item in result['files'][1:]]),
        file_all_ms=[item['upload_to_completed_observed_ms'] for item in result['files']],
        model=result['final_status']['model'], repeated_file_pcm_identical=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--realtime-reference', type=Path)
    args = parser.parse_args()
    root = args.root
    gpu, cpu = root/'feature-latency-native-gpu', root/'feature-latency-native'
    components = read(root/'feature-latency-components/results.json')
    recognition = read(root/'feature-latency-recognition/results.json')
    if any(run['first_partial_ms'] is None or not run['finals'] for run in recognition['asr_runs']):
        raise ValueError('在线识别测量没有文本')
    tts = {}
    for mode in ['preview', 'early playback']:
        runs = [item for item in components['speech_jobs'] if item['mode'] == mode]
        jobs = [read(root/'feature-latency-components/speech/results'/f'{item["job"]["id"]}.json') for item in runs]
        if any(job['status'] != 'completed' for job in jobs):
            raise ValueError('参考音色任务没有完成')
        checks = []
        for job in jobs:
            full = pcm(root/'feature-latency-components/speech/results'/f'{job["id"]}.wav')
            chunks = np.concatenate([pcm(root/'feature-latency-components/speech/results'/part['filename'])
                                     for part in job['chunks']])
            if not np.array_equal(full, chunks) or not np.isfinite(full).all():
                raise ValueError('参考音色完整输出与音频块不同')
            checks.append(dict(job=job['id'], samples=len(full), peak=float(np.max(abs(full))),
                               finite=True, ordered_blocks_match=True))
        tts[mode] = dict(count=len(jobs), first_ready=stats([(j['audio_ready_at']-j['created_at'])*1000 for j in jobs]),
            generation_complete=stats([(j['generation_finished_at']-j['created_at'])*1000 for j in jobs]),
            ready_all_ms=[(j['audio_ready_at']-j['created_at'])*1000 for j in jobs],
            playback_first=stats([(j['playback_started_at']-j['created_at'])*1000 for j in jobs]) if mode != 'preview' else None,
            playback_complete=stats([(j['finished_at']-j['created_at'])*1000 for j in jobs]) if mode != 'preview' else None,
            output_seconds=[j['duration_seconds'] for j in jobs], audio_checks=checks)
    dsp = read(root/'feature-latency-dsp.json')
    speech = read(root/'speech-analysis.json')
    result = dict(base_commit='531a18d', native_gpu=native(gpu), native_cpu_control=native(cpu),
        cancellation=read(root/'feature-latency-cancel/results.json'),
        file_cpu_vs_gpu=compare(pcm(cpu/'file-0.wav'), pcm(gpu/'file-0.wav')),
        speech_replay=dict(summary=speech['summary'], record_count=len(speech['records']),
            validation={key: value for key, value in speech['validation'].items()
                        if key not in {'protocol', 'parents', 'wave_checks'}}),
        components=dict(summary={key: value for key, value in components['summary'].items()
                                 if not key.startswith(('ASR', 'SenseVoice'))}, tts=tts,
            initial_asr_negative_probe=dict(input='qiqi-espeak.wav', runs=3, nonempty_online_results=0),
            translator_ready_ms=components['translator_ready_ms'], speech_http_ready_ms=components['speech_http_ready_ms'],
            speech_runtime_ready_ms=components['speech_runtime_ready_ms'], speech_runtime=components['speech_runtime'],
            translation_failures=[item for item in components['translations'] if 'error' in item]),
        recognition=dict(summary=recognition['summary'], input=recognition['input'], runs=recognition['asr_runs'],
            first_partial=stats([r['first_partial_ms'] for r in recognition['asr_runs']]),
            input_end_to_last_final=stats([r['input_end_to_last_final_ms'] for r in recognition['asr_runs']]),
            refinement=recognition['refinement']),
        dsp=[dict(rnnoise=item['rnnoise'], constructor_ms=item['constructor_ms'], frames=stats(item['frame_ms']))
             for item in dsp['records']],
        evidence_sha256={str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file() and path.suffix in {'.json', '.jsonl', '.log'}},
        limitations=['单台 Jetson 使用 MAXN_SUPER；未清空磁盘缓存，未固定频率。',
            'HTTP 使用本机回环，不含 SSH、无线网络和浏览器绘制。',
            'UDP 时间戳标识契约包，不能表示缓冲音频内容的端到端延迟。',
            'ASR 断句计时使用文件采样边界，未使用人工标注的实际语音边界。',
            'null 播放按时长等待。USB、模数转换、数模转换及扬声器延迟需要物理回环测量。',
            '首次 espeak 输入没有产生在线字幕。该负向测试已保留，不计入有效识别延迟。',
            '单次启动和两次热文件转换不能估计其他设备或长期运行的尾部延迟。'])
    if result['file_cpu_vs_gpu']['relative_rmse'] > .001 or result['file_cpu_vs_gpu']['cosine'] < .999:
        raise ValueError('CPU 与 GPU 文件输出偏差超过本次验证阈值')
    if args.realtime_reference:
        reference = pcm(args.realtime_reference)
        checks = {}
        for directory in [cpu, gpu]:
            output = pcm(directory/'rvc-output.wav')
            delay = read(directory/'results.json')['streams']['rvc']['status']['stream']['startup_output_underruns']*960
            checks[directory.name] = compare(reference, output[delay:delay+len(reference)])
            if not checks[directory.name]['exact']:
                raise ValueError('去除启动等待后，实时输出与已验证输出不同')
        result['realtime_reference'] = dict(path=str(args.realtime_reference), delay_samples=delay, checks=checks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({key: result[key] for key in ['file_cpu_vs_gpu', 'dsp']}, indent=2))


if __name__ == '__main__':
    main()
