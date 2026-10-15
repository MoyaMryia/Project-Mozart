#!/usr/bin/env python3
"""复用已生成的真实音频，核对低积压下的比例选择、恢复和完整播放。"""
import argparse
import json
from pathlib import Path
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from speech_service import SpeechService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with wave.open(str(args.input), 'rb') as audio:
        duration = audio.getnframes()/audio.getframerate()
    worker = args.output/'replay-worker.py'
    worker.write_text('''import json,sys,shutil,wave
print(json.dumps({'ready':True}),flush=True)
for line in sys.stdin:
 job=json.loads(line);shutil.copyfile(sys.argv[1],job['output'])
 with wave.open(job['output'],'rb') as w:
  duration=w.getnframes()/w.getframerate();rate=w.getframerate()
 print(json.dumps({'duration_seconds':duration,'sample_rate':rate}),flush=True)
''')
    service = SpeechService(args.output/'speech', [sys.executable, str(worker), str(args.input.resolve())],
        playback_device='null', delivery_policy='coverage', playback_tempo='flexible', capture_playback=True)
    service.voices['replay'] = dict(id='replay', revision='recorded-pcm', transcript='', engine='pocket', status='needs_preview')
    records = []
    requested = [1., 1.012, 1.025, 1.037, 1.05, 1., 1., 1., 1., 1.]
    expected = [1., 1.012, 1.025, 1.037, 1.05, 1.04, 1.03, 1.02, 1.01, 1.]
    try:
        for index, ratio in enumerate(requested):
            job = service.submit(dict(text='The laboratory is temporarily unavailable.', voice_id='replay',
                playback=True, live=True, source_duration_seconds=duration/ratio, source_emitted_at=time.time()))
            deadline = time.monotonic()+20
            while True:
                result = service.route('GET', f"/api/speech/jobs/{job['id']}", {})
                if result['status'] in ('completed', 'failed', 'cancelled', 'expired'):
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError('Policy playback did not finish')
                time.sleep(.01)
            if result['status'] != 'completed' or result['playback_tempo'] != expected[index]:
                raise RuntimeError(f'Unexpected policy result: {result}')
            records.append(result)
            (args.output/'policy.json').write_text(json.dumps(dict(
                note='Recorded PocketTTS WAV replay; synthesis bypassed; actual paced ALSA null playback',
                input=str(args.input), jobs=records), ensure_ascii=False, indent=2)+'\n')
    finally:
        service.close()
    print(json.dumps([r['playback_tempo'] for r in records]))


if __name__ == '__main__':
    main()
