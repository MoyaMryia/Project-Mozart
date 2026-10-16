#!/usr/bin/env python3
"""汇总同输入、不同变速上限的播放等待和严格核对结果。"""
import argparse
from collections import Counter
import json
import math
from pathlib import Path

from verify_tempo_playback import stats


def load(path):
    return json.loads(path.read_text())


def collect(root):
    runs = []
    baseline = None
    baseline_input = None
    for name in ['final105', 'final115']:
        directory = root/name
        analysis = load(directory/'speech-analysis.json')
        jobs = load(directory/'jobs.json')
        comparison = load(directory/'verification.json')['playback']
        campaign = load(directory/'campaign.json')
        signatures = load(directory/'source-signatures.json')
        if campaign['comparison_performed'] or campaign['all_pcm_identical'] is not None:
            raise ValueError('Expected a single-configuration RVC measurement')
        if not jobs or not all(j['status'] == 'completed' for j in jobs):
            raise ValueError('Missing or incomplete speech jobs')
        if len({j['id'] for j in jobs}) != len(jobs):
            raise ValueError('Duplicate speech job IDs')
        checks = comparison['checks']
        expected = {j['id'] for j in jobs if j.get('playback_audio_filename') or j['playback_tempo'] > 1}
        actual = [c['job'] for c in checks]
        if len(actual) != len(expected) or set(actual) != expected or not all(
                c['exact'] is True and c['ffmpeg_exact'] is True and
                (c['native_exact'] is True or c['native_exact'] is None) and
                c['rate_matches'] is True and c['metadata_matches'] is True for c in checks):
            raise ValueError('Independent playback verification failed')
        if baseline is None:
            baseline = signatures
        if signatures != baseline:
            raise ValueError('Source text or synthesis audio changed between runs')
        delays = [p['first_play_minus_final_emit_seconds'] for p in analysis['parents']]
        if not delays or any(d is None or not math.isfinite(d) for d in delays):
            raise ValueError('Missing or non-finite parent playback timestamps')
        source_sha256 = analysis['protocol']['source_sha256']
        if baseline_input is None:
            baseline_input = source_sha256
        if source_sha256 != baseline_input:
            raise ValueError('Source audio SHA-256 changed between runs')
        runs.append(dict(case=name, source_sha256=analysis['protocol']['source_sha256'],
            parent_first_pcm_seconds=stats(delays), parents=analysis['parent_count'],
            completed_parents=analysis['completed_speech_parents'], jobs=len(jobs),
            job_states=dict(Counter(j['status'] for j in jobs)),
            identical_text_source_and_wav_to_baseline=True,
            synthesis_audio_seconds=sum(j['duration_seconds'] for j in jobs),
            playback_audio_seconds=comparison['total_playback_seconds'],
            tempo_counts=dict(Counter(str(j['playback_tempo']) for j in jobs)),
            limited_jobs=sum(j['tempo_decision']['limited'] for j in jobs),
            minimum_available_GiB=analysis['minimum_available_GiB'],
            asr_frames=analysis['native_frames'], asr_overruns=analysis['native_overruns'],
            source_covered_parents=analysis['source_covered_parents'],
            playback_order_preserved=analysis['playback_order_preserved'],
            revision_violations=analysis['revision_violations'],
            final_speech_status=analysis['final_speech_status'],
            exit=analysis['exit'], playback_verification=comparison,
            rvc=campaign['runs'][0], source_signatures=signatures))
    return dict(runs=runs, median_reduction=1-runs[1]['parent_first_pcm_seconds']['median']/
                runs[0]['parent_first_pcm_seconds']['median'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = collect(args.input_dir)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({r['case']:r['parent_first_pcm_seconds'] for r in result['runs']}, indent=2))


if __name__ == '__main__':
    main()
