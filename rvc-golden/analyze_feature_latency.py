#!/usr/bin/env python3
"""从完整日志计算语音功能延迟，并保留每个样本。"""
import argparse
import json
from pathlib import Path
import sys

import numpy as np


def stats(values):
    return dict(count=len(values), median_ms=float(np.median(values)),
                p95_ms=float(np.percentile(values, 95)), max_ms=float(max(values)))


def analyze(root):
    sys.path.insert(0, str(Path(__file__).parent/'realtime-patches-20261008'))
    from analyze_clauses import analyze as validate
    from analyze_boundaries import classifier_results
    validation = classifier_results(root, validate(root))
    rows = [json.loads(line) for line in (root/'runtime/subtitles.jsonl').read_text().splitlines()]
    observed = [json.loads(line) for line in (root/'sse-observed.jsonl').read_text().splitlines()]
    jobs = json.loads((root/'jobs.json').read_text())
    latest, source_events, first_partial = {}, {}, {}
    for row in rows:
        latest[row['seq']] = row
        key = (row['seq'], row['asr_emitted_at'], row['final'])
        source_events.setdefault(key, row)
        if not row['final']:
            first_partial.setdefault(row['seq'], row)
    offset = json.loads((root/'protocol.json').read_text())['wall_minus_monotonic_seconds']
    records = []

    def add(feature, seconds, **identity):
        records.append(dict(feature=feature, elapsed_ms=seconds*1000, **identity))

    for seq, row in first_partial.items():
        add('asr input start to first partial', row['asr_emitted_at']-(offset+row['audio_start_pts_ns']/1e9), seq=seq)
    for row in source_events.values():
        add('asr event to bridge receive', row['received_at']-row['asr_emitted_at'], seq=row['seq'], revision=row['revision'])
        if row['final']:
            add('asr last captured packet end to final', row['asr_emitted_at']-(offset+row['audio_end_pts_ns']/1e9), seq=row['seq'])
    seen = set()
    for item in observed:
        row = item['event']
        key = (row['seq'], row['asr_emitted_at'], row['final'])
        if key not in source_events or key in seen:
            continue
        original = source_events[key]
        seen.add(key)
        add('bridge source record to SSE observed', item['observed_at']-original['received_at'], seq=row['seq'])
        add('asr event to SSE observed', item['observed_at']-row['asr_emitted_at'], seq=row['seq'])
    for parent in latest.values():
        for part in parent['translation_segments']:
            identity = dict(seq=parent['seq'], part=part['index'], state=part['translation_status'],
                            attempts=len(part.get('translation_audit', [])), early=part.get('early', False))
            add('translation compute including retries', part['translate_ms']/1000, **identity)
            add('translation submit to result', part['translated_at']-part['submitted_at'], **identity)
            add('translation wait and dispatch overhead',
                part['translated_at']-part['submitted_at']-part['translate_ms']/1000, **identity)
            if part['translation_status'] == 'completed' and len(part.get('translation_audit', [])) == 1:
                add('translation successful one attempt', part['translate_ms']/1000, **identity)
    for job in jobs:
        if job['status'] != 'completed':
            continue
        identity = dict(job=job['id'], utterance=job['utterance_id'])
        add('speech queue', job['started_at']-job['created_at'], **identity)
        add('speech start to first ready', job['audio_ready_at']-job['started_at'], **identity)
        add('speech ready to first playback', job['playback_started_at']-job['audio_ready_at'], **identity)
        add('speech submit to first playback', job['playback_started_at']-job['created_at'], **identity)
        add('speech submit to generation complete', job['generation_finished_at']-job['created_at'], **identity)
        add('speech submit to playback complete', job['finished_at']-job['created_at'], **identity)
    for parent in validation['parents']:
        elapsed = parent['first_play_minus_final_emit_seconds']
        if elapsed is not None:
            add('parent final ASR to first playback', elapsed, seq=parent['seq'])
    return dict(validation=validation, records=records,
                summary={key: stats([r['elapsed_ms'] for r in records if r['feature'] == key])
                         for key in sorted({r['feature'] for r in records})},
                notes=['SSE 计时止于 Python 客户端接收正文，不含浏览器绘制。',
                       'ASR 已捕获帧末尾包含断句静音，不代表实际语音末尾。',
                       '播放时间戳来自按音频时长等待的 ALSA null，不含物理设备延迟。',
                       '翻译计时包含检查与重试。等待项包含调度开销及毫秒取整误差。'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = analyze(args.root)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(result['summary'], indent=2))
