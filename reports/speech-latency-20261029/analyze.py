#!/usr/bin/env python3
"""从已保存的任务与字幕记录重算阶段耗时，不启动服务。"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics


def summary(values, total):
    values = sorted(values)
    return {'count': len(values), 'missing': total-len(values),
            'median_ms': statistics.median(values) if values else None,
            'p95_ms': values[int(.95*(len(values)-1))] if values else None,
            'max_ms': max(values) if values else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, help='包含 jobs.json、protocol.json、exit-status.json 和 runtime/subtitles.jsonl 的归档目录')
    args = parser.parse_args()
    inputs = {name: (args.root/name).read_bytes() for name in
              ('jobs.json', 'protocol.json', 'runtime/subtitles.jsonl', 'exit-status.json')}
    jobs = json.loads(inputs['jobs.json'])
    protocol = json.loads(inputs['protocol.json'])
    rows = [json.loads(line) for line in inputs['runtime/subtitles.jsonl'].splitlines() if line.strip()]
    latest, finals, first_partial, first_play = {}, {}, {}, {}
    for row in rows:
        key = row['utterance_id']
        latest[key] = row
        if row.get('final'):
            finals[key] = row
        elif row.get('received_at') is not None:
            first_partial.setdefault(key, row['received_at'])
    segments = [segment for row in latest.values() for segment in row.get('translation_segments', [])]
    translated_by_job = {segment['speech']: segment['translated_at'] for segment in segments
                         if segment.get('speech') and segment.get('translated_at') is not None}
    delivery_times = {job['id']: (job['delivery_created_at'] if job.get('delivery_created_at') is not None
                                 else translated_by_job.get(job['id'])) for job in jobs}
    delivery_basis = Counter('job_delivery_created_at' if job.get('delivery_created_at') is not None
                             else 'matching_caption_translation' if delivery_times[job['id']] is not None
                             else 'missing' for job in jobs)
    metrics = {'delivery_queue_and_http': summary([
        (job['created_at']-delivery_times[job['id']])*1000 for job in jobs
        if delivery_times[job['id']] is not None], len(jobs))}
    for label, start, end in (
            ('speech_queue', 'created_at', 'started_at'),
            ('processing_to_first_audio', 'started_at', 'audio_ready_at'),
            ('audio_ready_to_playback', 'audio_ready_at', 'playback_started_at'),
            ('speech_admission_to_playback', 'created_at', 'playback_started_at')):
        metrics[label] = summary([(job[end]-job[start])*1000 for job in jobs
                                  if job.get(start) is not None and job.get(end) is not None], len(jobs))
    for job in jobs:
        if job.get('playback_started_at') is not None:
            parent = job['utterance_id'].partition('/part/')[0]
            first_play[parent] = min(first_play.get(parent, job['playback_started_at']), job['playback_started_at'])
    metrics['final_asr_to_first_playback'] = summary([
        (first_play[key]-row['asr_emitted_at'])*1000 for key, row in finals.items()
        if key in first_play and row.get('asr_emitted_at') is not None], len(finals))
    metrics['first_partial_to_final'] = summary([
        (row['asr_emitted_at']-first_partial[key])*1000 for key, row in finals.items()
        if key in first_partial and row.get('asr_emitted_at') is not None], len(finals))
    metrics['translation_request_with_checks'] = summary([
        segment['translate_ms'] for segment in segments if segment.get('translate_ms') is not None], len(segments))
    timed = [job for job in jobs if all(job.get(name) is not None for name in
             ('created_at', 'started_at', 'audio_ready_at', 'playback_started_at'))]
    worst = max(timed, key=lambda job: job['playback_started_at']-job['created_at']) if timed else None
    worst_timeline = None if worst is None else {
        'id': worst['id'], 'utterance_id': worst['utterance_id'],
        'queue_ms': (worst['started_at']-worst['created_at'])*1000,
        'processing_to_audio_ms': (worst['audio_ready_at']-worst['started_at'])*1000,
        'audio_to_playback_ms': (worst['playback_started_at']-worst['audio_ready_at'])*1000,
        'total_ms': (worst['playback_started_at']-worst['created_at'])*1000}
    report = {'method': 'Offline wall-clock recalculation; percentile rule: analyze.py summary().',
              'analysis_script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'delivery_timing_basis': dict(delivery_basis),
              'source_root': str(args.root), 'source_sha256': {
                  name: hashlib.sha256(data).hexdigest() for name, data in inputs.items()},
              'requested_source_seconds': protocol.get('source_seconds'),
              'speech_command': protocol.get('commands', {}).get('speech'),
              'caption_command': protocol.get('commands', {}).get('captions'),
              'caption_rows': len(rows), 'final_parents': len(finals), 'jobs': len(jobs),
              'archival_exit': json.loads(inputs['exit-status.json']),
              'job_states': dict(Counter(job['status'] for job in jobs)),
              'worst_job_timeline': worst_timeline, 'metrics': metrics}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
