#!/usr/bin/env python3
"""Run the retained translated-speech probe beside a realtime RVC replay."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--boundary-model', type=Path, required=True)
    parser.add_argument('--label', default='efficiency-coexistence-20261014')
    parser.add_argument('--tts-audio-chunk-frames', type=int, choices=[3,5,10,15], default=3)
    parser.add_argument('--tts-threads', type=int, default=2)
    parser.add_argument('--commit', default='1e4d8e5+latency-working')
    args = parser.parse_args()
    home, root = Path.home(), args.repo.resolve()
    for name, target in [('.venv', home/'Mozart/.venv'), ('build-gpu', args.build.resolve())]:
        link = root/name
        if not link.exists():
            link.symlink_to(target, target_is_directory=True)
        if link.resolve() != target.resolve():
            raise RuntimeError(f'Independent checkout link differs: {link}')
    args.work.mkdir(parents=True, exist_ok=True)
    source = home/'Mozart/rvc-golden/sentence-context-20261007/processed-source-120s.wav'
    probe = [str(root/'.venv/bin/python'), str(root/'rvc-golden/realtime-patches-20261008/run_probe.py'),
        str(root), args.label, '--commit', args.commit,
        '--early-audio', '--tts-pythonpath', str(home/'mozart-realtime-patches-20261008/early-audio-runtime-20261009/python'),
        '--tts-audio-chunk-frames', str(args.tts_audio_chunk_frames), '--tts-threads', str(args.tts_threads),
        '--boundary-model', str(args.boundary_model)]
    campaign = [sys.executable, '-u', str(root/'rvc-golden/run_efficiency_campaign.py'),
        '--baseline', str(args.build/'state/mozart_stated'), '--candidate', str(args.build/'state/mozart_stated'),
        '--repo', str(root), '--assets', str(home/'Mozart/rvc-golden/qiqi-zh-run'),
        '--input', str(source), '--output-dir', str(args.work/'rvc'), '--repeats', '1', '--cases', 'parallel',
        '--playout-delay-ms', '400']
    began = time.time()
    (args.work/'protocol.json').write_text(json.dumps(dict(probe=probe, campaign=campaign, started_at=began,
        topology='Separate native API processes; shared CPU, GPU and RAM; paced ALSA null speech output'), indent=2)+'\n')
    with (args.work/'speech-probe.log').open('w') as log:
        process = subprocess.Popen(probe, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic()+120
            while True:
                if process.poll() is not None:
                    raise RuntimeError('Translated-speech probe exited during startup')
                try:
                    with urllib.request.urlopen('http://127.0.0.1:18080/api/speech/status', timeout=2) as response:
                        status = json.load(response)
                    if status.get('session', {}).get('enabled'):
                        break
                except (OSError, ValueError):
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError('Translated-speech startup timed out')
                time.sleep(.2)
            with (args.work/'rvc-campaign.log').open('w') as rvc_log:
                subprocess.run(campaign, stdout=rvc_log, stderr=subprocess.STDOUT, timeout=180, check=True)
            process.wait(timeout=180)
            if process.returncode:
                raise RuntimeError('Translated-speech probe failed')
            result = root/'rvc-golden/realtime-patches-20261008'/args.label
            exit_status = json.loads((result/'exit-status.json').read_text())
            if exit_status['exit_code'] or exit_status['failure'] or exit_status['remaining_pids_before_cleanup']:
                raise RuntimeError(f'Invalid speech probe exit: {exit_status}')
            sys.path.insert(0, str(root/'rvc-golden/realtime-patches-20261008'))
            from analyze_clauses import analyze
            from analyze_boundaries import classifier_results
            analysis = classifier_results(result, analyze(result))
            (args.work/'speech-analysis.json').write_text(json.dumps(analysis, ensure_ascii=False, indent=2)+'\n')
            if (analysis['source_covered_parents'] != analysis['parent_count']
                or analysis['completed_speech_parents'] != analysis['parent_count']
                or analysis['revision_violations'] or not analysis['playback_order_preserved']
                or analysis['native_overruns']
                or any(not item['ordered_blocks_match'] or item['fullscale_samples'] for item in analysis['wave_checks'])
                or any(analysis['final_speech_status'].values())):
                raise RuntimeError('Speech coverage, PCM continuity, or queue validation failed')
            (args.work/'result.json').write_text(json.dumps(dict(speech_result=str(result),
                rvc_result=str(args.work/'rvc/campaign.json'), elapsed_seconds=time.time()-began), indent=2)+'\n')
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


if __name__ == '__main__':
    main()
