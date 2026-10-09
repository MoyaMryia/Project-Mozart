"""Compare retained early-audio controls and replay records."""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import statistics
import wave

import numpy as np
from analyze_probe import analyze


def stats(values):
    values = sorted(values)
    return {'count': len(values), 'median': statistics.median(values),
            'min': min(values), 'max': max(values)} if values else None


def pcm(path):
    with wave.open(str(path), 'rb') as source:
        assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, 24000)
        return source.readframes(source.getnframes())


def compare(left, right):
    a, b = pcm(left), pcm(right)
    result = {'identical': a == b, 'samples': [len(a)//2, len(b)//2],
              'pcm_sha256': [hashlib.sha256(x).hexdigest() for x in (a, b)]}
    if len(a) == len(b):
        x, y = [np.frombuffer(value, dtype='<i2').astype('float64')/32768 for value in (a, b)]
        delta = np.abs(x-y)
        result.update(max_absolute_error=float(delta.max()), mean_absolute_error=float(delta.mean()),
            cosine_similarity=float(np.dot(x, y)/(np.linalg.norm(x)*np.linalg.norm(y))),
            rms_ratio=float(np.linalg.norm(y)/np.linalg.norm(x)))
    return result


def fixed(root):
    output, reports = {}, {}
    for mode in ('installed', 'patched', 'early'):
        directory = root/f'early-audio-fixed-{mode}'
        report = json.loads((directory/'results.json').read_text())
        records = report['records']
        warm = [r for r in records if r['repeat'] == 1]
        output[mode] = {'runtime': report['runtime'], 'submitted': len(records),
            'provenance': {k: report[k] for k in ('command', 'worker_sha256', 'reference_sha256', 'texts_sha256', 'model_sha256', 'profile')},
            'completed': sum('error' not in r['result'] for r in records),
            'errors': [r['result']['error'] for r in records if 'error' in r['result']],
            'exit_code': report.get('exit_code'), 'startup_seconds': report['startup_seconds'],
            'warm_first_available_seconds': stats([r['first_audio_seconds'] for r in warm]),
            'warm_full_result_seconds': stats([r['roundtrip_seconds'] for r in warm]),
            'warm_rtf': stats([r['result']['synthesis_seconds']/r['result']['duration_seconds'] for r in warm]),
            'limited_samples': sum(r['result'].get('limited_samples', 0) for r in records)}
        provenance = directory/'build-provenance.json'
        if provenance.exists():
            output[mode]['binary_provenance'] = json.loads(provenance.read_text())
        reports[mode] = report
    comparisons = []
    for record in reports['early']['records']:
        name = f'{record["repeat"]:02d}-{record["index"]:02d}.wav'
        directory = root/'early-audio-fixed-early'
        comparisons.append({'name': name,
            'installed_to_patched': compare(root/'early-audio-fixed-installed'/name, root/'early-audio-fixed-patched'/name),
            'patched_to_early': compare(root/'early-audio-fixed-patched'/name, directory/name),
            'blocks_match_final': pcm(directory/name) == b''.join(pcm(directory/b['filename']) for b in record['audio_blocks']),
            'block_count': len(record['audio_blocks']),
            'first_available_seconds': record['first_audio_seconds'],
            'full_result_seconds': record['roundtrip_seconds']})
    return {'variants': output, 'comparisons': comparisons}


def replay(directory):
    with contextlib.redirect_stdout(io.StringIO()):
        analyze(directory)
    result = json.loads((directory/'analysis.json').read_text())
    result['protocol'] = json.loads((directory/'protocol.json').read_text())
    status = json.loads((directory/'runtime/speech-results.json').read_text())
    result['final_speech_status'] = {k: status[k] for k in ('runtime', 'unfinished_jobs', 'pending_jobs', 'storage_warning', 'warmup_error', 'streaming_playback')}
    result['exit'].pop('owned_processes', None)
    jobs = json.loads((directory/'jobs.json').read_text())
    within, between = [], []
    for job in jobs:
        chunks = job.get('chunks', [])
        for previous, current in zip(chunks, chunks[1:]):
            if 'finished_at' not in previous or 'playback_started_at' not in current:
                continue
            gap = current['playback_started_at']-previous['finished_at']
            target = within if previous.get('text_chunk_index') == current.get('text_chunk_index') and current.get('streamed') else between
            target.append(gap)
    result['within_text_block_gaps_seconds'] = stats(within)
    result['between_text_piece_gaps_seconds'] = stats(between)
    result['gaps_above_20ms'] = sum(v > .02 for v in within+between)
    result['limited_samples'] = sum(p.get('limited_samples', 0) for j in jobs for p in j.get('chunks', []))
    result['first_text_part_compute_seconds'] = stats([j['first_text_part_synthesis_seconds'] for j in jobs if 'first_text_part_synthesis_seconds' in j])
    result['generation_to_first_available_seconds'] = stats([j['audio_ready_at']-j['started_at'] for j in jobs if 'audio_ready_at' in j])
    result['generation_to_first_play_seconds'] = stats([j['playback_started_at']-j['started_at'] for j in jobs if 'playback_started_at' in j])
    result['ready_to_first_play_seconds'] = stats([j['playback_started_at']-j['audio_ready_at'] for j in jobs if 'playback_started_at' in j])
    result['ready_to_play_above_100ms'] = sum(j['playback_started_at']-j['audio_ready_at'] > .1 for j in jobs if 'playback_started_at' in j)
    result['text_piece_count'] = sum(len(j['chunk_texts']) for j in jobs)
    result['early_play_before_first_text_complete'] = sum(j.get('early_audio', False) and
        j['playback_started_at']-j['started_at'] < j.get('first_text_part_synthesis_seconds', 0) for j in jobs if 'playback_started_at' in j)
    return result, jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = {'date': '2026-10-09', 'application_commit': '91100e4', 'fixed': fixed(args.root)}
    base, base_jobs = replay(args.root/'early-audio-control-v1')
    early, early_jobs = replay(args.root/'early-audio-v1')
    result['replay'] = {'control': base, 'early': early}
    checks = []
    for a, b in zip(base_jobs, early_jobs):
        same_text = a['source_text'] == b['source_text'] and a['text'] == b['text']
        checks.append({'source_text': a['source_text'], 'same_text': same_text,
            'degraded': b.get('speech_degraded'),
            'audio': compare(args.root/'early-audio-control-v1/speech/results'/f'{a["id"]}.wav',
                args.root/'early-audio-v1/speech/results'/f'{b["id"]}.wav') if same_text and a['status'] == b['status'] == 'completed' else None})
    result['replay_comparisons'] = checks
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'fixed': result['fixed']['variants'], 'replay': result['replay']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
