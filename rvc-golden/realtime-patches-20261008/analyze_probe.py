"""Analyze retained replay records without running model inference."""
import argparse
import array
import collections
import json
from pathlib import Path
import re
import statistics
import wave


def stats(values):
    values = sorted(values)
    if not values:
        return None
    return {'count': len(values), 'median': statistics.median(values),
            'p95': values[min(len(values)-1, int(.95*len(values)))], 'max': max(values)}


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def analyze(root):
    protocol = json.loads((root/'protocol.json').read_text())
    rows = lines(root/'runtime/subtitles.jsonl')
    observed = lines(root/'sse-observed.jsonl')
    telemetry = lines(root/'telemetry.jsonl')
    jobs = json.loads((root/'jobs.json').read_text())
    latest, finals, partials = {}, {}, []
    previous = {}
    violations = []
    for row in rows:
        key = row['utterance_id']
        if row['revision'] <= previous.get(key, -1):
            violations.append(key)
        previous[key] = row['revision']
        latest[key] = row
        if not row['final']:
            partials.append(row)
        elif row['translation_status'] == 'pending' and key not in finals:
            finals[key] = row
    partial_delivery = [item['observed_at']-item['event']['asr_emitted_at'] for item in observed
                        if not item['event'].get('final') and item['event'].get('asr_emitted_at')]
    source_delivery = [r['received_at']-r['asr_emitted_at'] for r in partials]
    translated = [r for r in latest.values() if r['final'] and r['translation_status'] == 'completed']
    overlapping = sum(any(p['utterance_id'] != r['utterance_id'] and
                         finals[r['utterance_id']]['received_at'] < p['received_at'] < r['translated_at']
                         for p in partials) for r in translated if r['utterance_id'] in finals)
    completed = [j for j in jobs if j['status'] == 'completed']
    chunks = [p for j in jobs for p in j.get('chunks', [])]
    played = sorted([p for p in chunks if 'playback_started_at' in p], key=lambda p: p['playback_started_at'])
    job_order = {j['id']: i for i, j in enumerate(sorted(jobs, key=lambda j: j['created_at']))}
    actual_order = [(job_order[p['job_id']], p['index']) for p in played]
    wave_checks = []
    for job in completed:
        path = root/'speech/results'/f"{job['id']}.wav"
        with wave.open(str(path), 'rb') as source:
            pcm = source.readframes(source.getnframes())
            duration = source.getnframes()/source.getframerate()
            assert source.getsampwidth() == 2
        pieces = []
        for part in job['chunks']:
            with wave.open(str(root/'speech/results'/part['filename']), 'rb') as source:
                pieces.append(source.readframes(source.getnframes()))
        samples = array.array('h', pcm)
        wave_checks.append({'job_id': job['id'], 'duration_seconds': duration,
            'parent_matches_ordered_pieces': pcm == b''.join(pieces),
            'peak_int16': max(abs(v) for v in samples),
            'clipped_samples': sum(abs(v) >= 32767 for v in samples)})
    microphone = (root/'runtime/microphone.log').read_text()
    frames = re.findall(r'发送 (\d+) 帧.*overruns=(\d+)', microphone)
    gpu_log = (root/'tegrastats.log').read_text()
    gpu = [int(value) for value in re.findall(r'GR3D_FREQ\s+(\d+)%', gpu_log)]
    temperatures = [float(value) for value in re.findall(r'gpu@([0-9.]+)C', gpu_log)]
    injection_path = root/'failure-injection.json'
    injection = json.loads(injection_path.read_text()) if injection_path.exists() else None
    output = {'mode': protocol['mode'], 'exit': json.loads((root/'exit-status.json').read_text()),
        'caption_record_count': len(rows), 'unique_utterances': len(latest), 'final_utterances': len(finals),
        'partial_updates': len(partials), 'revision_violations': violations,
        'translation_status': dict(collections.Counter(r['translation_status'] for r in latest.values())),
        'refinement_status': dict(collections.Counter(r.get('refinement_status', 'none') for r in latest.values())),
        'partial_asr_emit_to_bridge_seconds': stats(source_delivery),
        'partial_asr_emit_to_sse_seconds': stats(partial_delivery),
        'translation_compute_seconds': stats([r['translate_ms']/1000 for r in translated]),
        'final_emit_to_translation_seconds': stats([r['translated_at']-r['asr_emitted_at'] for r in translated]),
        'refinement_compute_seconds': stats([r['refinement_decode_ms']/1000 for r in latest.values() if r.get('refinement_status') == 'completed']),
        'translations_with_other_partial_updates_while_pending': overlapping,
        'job_states': dict(collections.Counter(j['status'] for j in jobs)), 'job_count': len(jobs),
        'source_to_speech_errors': dict(collections.Counter(r['speech_error'] for r in latest.values() if r.get('speech_error'))),
        'translation_errors': dict(collections.Counter(r['translation_error'] for r in latest.values() if r.get('translation_error'))),
        'submit_to_first_play_seconds': stats([j['playback_started_at']-j['created_at'] for j in completed]),
        'final_emit_to_first_play_seconds': stats([j['playback_started_at']-finals[j['utterance_id']]['asr_emitted_at'] for j in completed if j.get('utterance_id') in finals]),
        'first_piece_compute_seconds': stats([j['chunks'][0]['synthesis_seconds'] for j in completed]),
        'played_piece_count': len(played), 'playback_order_preserved': actual_order == sorted(actual_order),
        'completed_wave_checks': wave_checks,
        'native_frames': int(frames[-1][0]) if frames else None, 'native_overruns': int(frames[-1][1]) if frames else None,
        'minimum_available_GiB': min(r['available_bytes'] for r in telemetry)/1024**3,
        'cpu_all_cores_percent': stats([r['cpu_all_cores_percent'] for r in telemetry if r['cpu_all_cores_percent'] is not None]),
        'max_swap_used_bytes': max(r['swap_used_bytes'] for r in telemetry),
        'swapin_pages': telemetry[-1]['pswpin']-telemetry[0]['pswpin'],
        'gpu_percent': stats(gpu), 'gpu_mean_percent': statistics.mean(gpu) if gpu else None,
        'gpu_nonzero_sample_fraction': sum(v > 0 for v in gpu)/len(gpu) if gpu else None,
        'gpu_temperature_C': stats(temperatures),
        'max_buffered_audio_seconds': max((r.get('buffered_audio_seconds') or 0 for r in telemetry), default=0),
        'finals_after_failure': sum(r['asr_emitted_at'] > injection['at'] for r in finals.values()) if injection else None,
        'partial_updates_after_failure': sum(r['asr_emitted_at'] > injection['at'] for r in partials) if injection else None}
    (root/'analysis.json').write_text(json.dumps(output, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k: v for k, v in output.items() if k not in ('exit', 'completed_wave_checks')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    analyze(parser.parse_args().root)
