#!/usr/bin/env python3
"""比较早期音频块大小，保留首块时间、完整时间和 PCM 差异。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import soundfile as sf
from run_efficiency_campaign import stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = json.loads((args.repo/'rvc-golden/realtime-patches-20261008/pocket-gpu-texts.json').read_text())
    texts = args.output/'texts.json'
    texts.write_text(json.dumps([source[i] for i in [0, 1, 2, 7]], indent=2)+'\n')
    home = Path.home()
    baseline = {}
    summary = []
    for name, frames, threads in [('baseline',15,2), ('five',5,2), ('three',3,2),
                                  ('ten',10,2), ('five-four-threads',5,4), ('baseline-repeat',15,2)]:
        command = [sys.executable, str(args.repo/'rvc-golden/realtime-patches-20261008/pocket_gpu_probe.py'),
            '--root', str(args.repo), '--model', str(home/'models/sherpa-onnx/clone-test-downloads/sherpa-onnx-pocket-tts-int8-2026-01-26'),
            '--reference', str(home/'Mozart/rvc-golden/clone-tts/qiqi-20261004/reference-qiqi-natural-source.wav'),
            '--texts', str(texts), '--output', str(args.output/name), '--provider', 'cpu', '--precision', 'int8',
            '--pythonpath', str(home/'mozart-realtime-patches-20261008/early-audio-runtime-20261009/python'),
            '--threads', str(threads), '--early-audio', '--audio-chunk-frames', str(frames), '--repeats', '2']
        with (args.output/(name+'.log')).open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
        result = json.loads((args.output/name/'results.json').read_text())
        comparisons = []
        for row in result['records']:
            if 'error' in row['result'] or not row.get('audio', {}).get('finite'):
                raise ValueError('早期音频块试验没有完成有效输出')
            value, rate = sf.read(row['request']['output'], dtype='float64')
            if name == 'baseline':
                baseline[row['index']] = value
            reference = baseline[row['index']]
            if len(value) != len(reference):
                raise ValueError('音频块试验改变了输出长度')
            comparisons.append(dict(index=row['index'], repeat=row['repeat'],
                exact=bool(np.array_equal(value, reference)),
                relative_rmse=float(np.linalg.norm(value-reference)/max(np.linalg.norm(reference),1e-30)),
                pcm_sha256=hashlib.sha256(value.tobytes()).hexdigest()))
        item = dict(case=name, frames=frames, threads=threads,
            first_audio=stats([r['first_audio_seconds']*1000 for r in result['records']]),
            complete=stats([r['roundtrip_seconds']*1000 for r in result['records']]),
            comparisons=comparisons, total_output_seconds=sum(r['result']['duration_seconds'] for r in result['records']))
        summary.append(item)
        (args.output/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
        print(json.dumps(item), flush=True)


if __name__ == '__main__':
    main()
