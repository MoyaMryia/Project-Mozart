"""Compare parent captions, source coverage, speech order, and replay delays."""
import argparse
import array
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import statistics
import wave


def stats(values):
    return {'count': len(values), 'median': statistics.median(values), 'max': max(values),
            'min': min(values)} if values else None


def sequence(key):
    return int(key.rsplit(':', 1)[-1].split('/part/')[0])


def pcm(path):
    with wave.open(str(path), 'rb') as source:
        assert source.getnchannels() == 1 and source.getsampwidth() == 2
        return source.readframes(source.getnframes()), source.getnframes()/source.getframerate()


def analyze(root):
    latest, previous, violations = {}, {}, []
    raw = [json.loads(line) for line in (root/'runtime/subtitles.jsonl').read_text().splitlines()]
    for row in raw:
        seq = row['seq']
        if row['revision'] <= previous.get(seq, -1):
            violations.append(seq)
        previous[seq], latest[seq] = row['revision'], row
    parents = [latest[k] for k in sorted(latest)]
    jobs = json.loads((root/'jobs.json').read_text())
    grouped = defaultdict(list)
    checks, all_chunks = [], []
    for job in jobs:
        grouped[sequence(job['utterance_id'])].append(job)
        all_chunks.extend(job.get('chunks', []))
        if job['status'] != 'completed':
            continue
        data, duration = pcm(root/'speech/results'/f'{job["id"]}.wav')
        blocks = [pcm(root/'speech/results'/part['filename'])[0] for part in job['chunks']]
        samples = array.array('h', data)
        checks.append({'job_id': job['id'], 'duration_seconds': duration,
            'ordered_blocks_match': data == b''.join(blocks),
            'fullscale_samples': sum(abs(s) >= 32767 for s in samples),
            'pcm_sha256': hashlib.sha256(data).hexdigest()})
    delays, early_submit_leads, gaps, parent_rows = [], [], [], []
    completed_parents = 0
    for parent in parents:
        items = grouped[parent['seq']]
        parts = parent.get('translation_segments', [])
        completed = bool(items) and all(j['status'] == 'completed' for j in items)
        if parts:
            completed = completed and len(items) == len(parts)
        completed_parents += int(completed)
        starts = [j['playback_started_at'] for j in items if 'playback_started_at' in j]
        delay = min(starts)-parent['asr_emitted_at'] if starts else None
        if completed and delay is not None:
            delays.append(delay)
        corrections = [p for p in parts if p.get('source_correction')]
        covered = (corrections[-1]['source_text'] if corrections else ''.join(p['source_text'] for p in parts)) if parts else parent['zh']
        expected_start = 0
        contiguous = True
        for part in parts:
            if not part.get('source_correction'):
                contiguous &= part['source_start'] == expected_start
            contiguous &= part['source_text'] == parent['zh'][part['source_start']:part['source_end']]
            expected_start = part['source_end']
            if part.get('early'):
                early_submit_leads.append(parent['asr_emitted_at']-part['submitted_at'])
        ordered_chunks = sorted([c for j in items for c in j.get('chunks', []) if 'playback_started_at' in c],
                                key=lambda c: c['playback_started_at'])
        for a, b in zip(ordered_chunks, ordered_chunks[1:]):
            if 'finished_at' in a:
                gaps.append(b['playback_started_at']-a['finished_at'])
        parent_rows.append({'seq': parent['seq'], 'source': parent['zh'], 'translation': parent['en'],
            'translation_status': parent['translation_status'], 'translation_error': parent.get('translation_error'),
            'source_revision_warning': parent.get('source_revision_warning'),
            'source_covered': covered == parent['zh'] and contiguous,
            'speech_completed': completed, 'first_play_minus_final_emit_seconds': delay,
            'segments': parts})
    played = sorted([c for c in all_chunks if 'playback_started_at' in c], key=lambda c: c['playback_started_at'])
    order = {j['id']: n for n, j in enumerate(sorted(jobs, key=lambda j: j['created_at']))}
    actual = [(order[c['job_id']], c['index']) for c in played]
    telemetry = [json.loads(line) for line in (root/'telemetry.jsonl').read_text().splitlines()]
    frames = re.findall(r'发送 (\d+) 帧.*overruns=(\d+)', (root/'runtime/microphone.log').read_text())
    status = json.loads((root/'runtime/speech-results.json').read_text())
    exit_status = json.loads((root/'exit-status.json').read_text())
    exit_status.pop('owned_processes', None)
    return {'protocol': json.loads((root/'protocol.json').read_text()), 'exit': exit_status,
        'parent_count': len(parents), 'final_parents': sum(p.get('final', False) for p in parents),
        'translation_parent_states': dict(Counter(p['translation_status'] for p in parents)),
        'source_covered_parents': sum(p['source_covered'] for p in parent_rows),
        'completed_speech_parents': completed_parents, 'job_states': dict(Counter(j['status'] for j in jobs)),
        'early_segments': len(early_submit_leads), 'early_submit_lead_seconds': stats(early_submit_leads),
        'first_play_minus_final_emit_seconds': stats(delays),
        'speech_before_final_parents': sum(d < 0 for d in delays),
        'revision_violations': violations, 'playback_order_preserved': actual == sorted(actual),
        'parent_internal_gaps_seconds': stats(gaps), 'parent_internal_gaps_above_20ms': sum(g > .02 for g in gaps),
        'output_duration_seconds': sum(c['duration_seconds'] for c in checks),
        'output_words': sum(len(j['text'].split()) for j in jobs),
        'wave_checks': checks, 'parents': parent_rows,
        'minimum_available_GiB': min(t['available_bytes'] for t in telemetry)/1024**3,
        'native_frames': int(frames[-1][0]) if frames else None, 'native_overruns': int(frames[-1][1]) if frames else None,
        'final_speech_status': {k: status.get(k) for k in ('unfinished_jobs', 'pending_jobs', 'storage_warning', 'warmup_error')}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('control', type=Path)
    parser.add_argument('experiment', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    data = {'control': analyze(args.control), 'experiment': analyze(args.experiment)}
    left, right = data['control'], data['experiment']
    assert [p['source'] for p in left['parents']] == [p['source'] for p in right['parents']]
    data['paired_parent_delay_deltas_seconds'] = stats([
        b['first_play_minus_final_emit_seconds']-a['first_play_minus_final_emit_seconds']
        for a, b in zip(left['parents'], right['parents'])
        if a['first_play_minus_final_emit_seconds'] is not None and b['first_play_minus_final_emit_seconds'] is not None])
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    for mode, result in [('control', left), ('experiment', right)]:
        print(mode, json.dumps({k: v for k, v in result.items() if k not in ('protocol', 'parents', 'wave_checks')}, ensure_ascii=False))
