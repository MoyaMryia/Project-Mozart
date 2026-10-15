#!/usr/bin/env python3
"""核对实际播放捕获、完整输入过滤结果、首音延迟和进程启动成本。"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import time
import wave


def pcm(path):
    with wave.open(str(path), 'rb') as source:
        if (source.getnchannels(), source.getsampwidth()) != (1, 2):
            raise ValueError('Expected mono PCM16')
        return source.readframes(source.getnframes()), source.getframerate()


def stats(values):
    values = sorted(values)
    return dict(count=len(values), median=statistics.median(values), min=values[0],
        p95=values[min(len(values)-1, int(.95*(len(values)-1)))], max=values[-1]) if values else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replay', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--helper', type=Path)
    args = parser.parse_args()
    result = {}
    if args.helper:
        records = []
        for iteration in range(6):
            for backend in (('ffmpeg','native') if iteration%2 == 0 else ('native','ffmpeg')):
                command = [str(args.helper),'24000','1.05'] if backend == 'native' else [
                    'ffmpeg','-v','error','-nostdin','-threads','1','-filter_threads','1',
                    '-probesize','32','-analyzeduration','0','-f','s16le','-ar','24000','-ac','1',
                    '-blocksize','1024','-i','pipe:0','-af','atempo=1.05','-f','s16le','-flush_packets','1','pipe:1']
                began = time.monotonic()
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                try:
                    process.stdin.write(b'\x10\x00'*5760)
                    process.stdin.flush()
                    if not process.stdout.read(1024):
                        raise RuntimeError('No filtered audio before input EOF')
                    first_ms = (time.monotonic()-began)*1000
                    process.communicate(timeout=5)
                    if process.returncode:
                        raise RuntimeError('Filter failed')
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                records.append(dict(backend=backend, iteration=iteration, first_pcm_ms=first_ms))
        result['startup'] = dict(records=records,
            warmed={backend:stats([r['first_pcm_ms'] for r in records if r['backend']==backend and r['iteration']])
                for backend in ('native','ffmpeg')})
    if args.replay:
        jobs = json.loads((args.replay/'jobs.json').read_text())
        checks = []
        for job in jobs:
            if not job.get('playback_audio_filename'):
                continue
            source, rate = pcm(args.replay/'speech/results'/f"{job['id']}.wav")
            actual, actual_rate = pcm(args.replay/'speech/results'/job['playback_audio_filename'])
            expected = subprocess.run(['ffmpeg','-v','error','-f','s16le','-ar',str(rate),'-ac','1',
                '-i','pipe:0','-af',f"atempo={job['playback_tempo']:.2f}",'-f','s16le','pipe:1'],
                input=source,stdout=subprocess.PIPE,check=True).stdout
            checks.append(dict(job=job['id'], exact=actual==expected,
                rate_matches=rate==actual_rate, samples=len(actual)//2,
                metadata_matches=len(actual)//2==job['playback_output_samples'],
                pcm_sha256=hashlib.sha256(actual).hexdigest(),
                first_pcm_after_input_ms=(job['playback_started_at']-job['playback_input_started_at'])*1000))
        result['playback'] = dict(captured_jobs=len(checks), checks=checks,
            tempo_counts=dict(Counter(str(j.get('playback_tempo',1)) for j in jobs)),
            first_pcm_after_input_ms=stats([r['first_pcm_after_input_ms'] for r in checks]),
            total_source_seconds=sum(j['duration_seconds'] for j in jobs),
            total_playback_seconds=sum(j.get('playback_duration_seconds',j['duration_seconds']) for j in jobs))
        if not checks or not all(r['exact'] and r['rate_matches'] and r['metadata_matches'] for r in checks):
            args.output.write_text(json.dumps(result,indent=2)+'\n')
            raise RuntimeError('Playback PCM verification failed')
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({key:{k:v for k,v in value.items() if k not in ('records','checks')} for key,value in result.items()},indent=2))


if __name__ == '__main__':
    main()
