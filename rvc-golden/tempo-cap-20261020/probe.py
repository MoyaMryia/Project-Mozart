#!/usr/bin/env python3
"""评估高于 1.15 倍的播放比例是否保持识别文本与音高。

实验脚本，不修改仓库代码。基准配置与 reports/tempo-limit-20261016 相同。
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

import numpy as np
import soundfile as sf


def normalise(text):
    return re.sub(r'\W', '', text.lower())


def asr_pass(args):
    import sherpa_onnx as so
    model = Path.home()/'models/sherpa-onnx/clone-test-downloads/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'
    recognizer = so.OfflineRecognizer.from_sense_voice(model=str(model/'model.int8.onnx'),
        tokens=str(model/'tokens.txt'), num_threads=2, language='en', use_itn=True)
    requested = {f'00-{index:02d}.wav': text
                 for index, text in enumerate(json.loads(Path(args.texts).read_text()))}
    args.output.mkdir(parents=True, exist_ok=True)
    records, references, correct = [], {}, {}
    for tempo in args.tempos:
        for path in sorted(args.input.glob('00-*.wav')):
            if not re.fullmatch(r'00-\d{2}\.wav', path.name):
                continue
            output = args.output/f'{path.stem}-tempo-{tempo:.3f}.wav'
            began = time.monotonic()
            if tempo == 1:
                shutil.copyfile(path, output)
            else:
                subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(path),
                    '-af', f'atempo={tempo}', '-c:a', 'pcm_s16le', str(output)], check=True)
            elapsed = time.monotonic()-began
            audio, rate = sf.read(output, dtype='float32')
            stream = recognizer.create_stream()
            samples = np.interp(np.arange(0, len(audio), rate/16000), np.arange(len(audio)), audio).astype(np.float32)
            stream.accept_waveform(16000, samples)
            recognizer.decode_stream(stream)
            text = stream.result.text.strip()
            want = requested[path.name]
            if tempo == 1:
                references[path.name] = text
                correct[path.name] = normalise(text) == normalise(want)
            ref = references[path.name]
            records.append(dict(name=path.name, input=str(path), output=str(output), tempo=tempo,
                seconds=len(audio)/rate, rate=rate, processing_seconds=elapsed,
                transcript=text, reference_transcript=ref, requested_text=want,
                same_transcript=normalise(text) == normalise(ref),
                baseline_correct=correct[path.name],
                rms=float(np.sqrt(np.mean(audio**2))), peak=float(np.max(abs(audio))),
                fullscale_samples=int(np.count_nonzero(abs(audio) >= 32767/32768))))
            (args.output/'quality.json').write_text(json.dumps(records, ensure_ascii=False, indent=2)+'\n')
    return records


def rmvpe_pass(args, records):
    import torch
    from scipy.signal import resample_poly
    import sys
    sys.path.insert(0, str(Path.home()/'mozart-archive/RVC'))
    from infer.rmvpe import RMVPE
    torch.set_num_threads(2)
    model = RMVPE(str(Path.home()/'Mozart/rvc-golden/assets/rmvpe/rmvpe.pt'), is_half=False, device='cpu')
    baseline = {}
    for row in records:
        audio, rate = sf.read(row['output'], dtype='float32')
        div = int(np.gcd(rate, 16000))
        pitch = model.infer_from_audio(resample_poly(audio, 16000//div, rate//div), thred=.03)
        if row['tempo'] == 1:
            baseline[row['name']] = pitch
        ref = baseline[row['name']]
        positions = np.minimum(len(ref)-1, np.rint(np.arange(len(pitch))*row['tempo']).astype(int))
        ref = ref[positions]
        both, either = (ref > 0) & (pitch > 0), (ref > 0) | (pitch > 0)
        cents = abs(1200*np.log2(pitch[both]/ref[both]))
        row.update(f0_method='official PyTorch RMVPE, CPU, 16 kHz, threshold 0.03',
            f0_median_cents=float(np.median(cents)) if len(cents) else None,
            f0_p90_cents=float(np.percentile(cents, 90)) if len(cents) else None,
            voiced_iou=float(np.count_nonzero(both)/max(1, np.count_nonzero(either))))
    (args.output/'quality-rmvpe.json').write_text(json.dumps(records, ensure_ascii=False, indent=2)+'\n')


def summarise(records):
    table = []
    for tempo in sorted({r['tempo'] for r in records}):
        rows = [r for r in records if r['tempo'] == tempo]
        correct = [r for r in rows if r['baseline_correct']]
        table.append(dict(tempo=tempo, files=len(rows), baseline_correct=len(correct),
            unchanged=[r['name'] for r in correct if r['same_transcript']],
            unchanged_count=sum(r['same_transcript'] for r in correct),
            changed=[dict(name=r['name'], transcript=r['transcript'], reference=r['reference_transcript'])
                     for r in correct if not r['same_transcript']],
            all_changed=sum(not r['same_transcript'] for r in rows),
            fullscale=sum(r['fullscale_samples'] for r in rows),
            seconds=round(sum(r['seconds'] for r in rows), 3),
            f0_median_cents_max=max((r['f0_median_cents'] or 0) for r in rows),
            f0_p90_cents_max=max((r['f0_p90_cents'] or 0) for r in rows)))
    return table


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--texts', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tempos', type=float, nargs='+', required=True)
    args = parser.parse_args()
    if args.tempos[0] != 1:
        parser.error('first tempo must be 1')
    records = asr_pass(args)
    rmvpe_pass(args, records)
    summary = summarise(records)
    (args.output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
    for row in summary:
        print(json.dumps(dict(tempo=row['tempo'], unchanged=f"{row['unchanged_count']}/{row['baseline_correct']}",
            all_changed=row['all_changed'], fullscale=row['fullscale'], seconds=row['seconds'],
            f0_median_max=round(row['f0_median_cents_max'], 2), f0_p90_max=round(row['f0_p90_cents_max'], 2)),
            ensure_ascii=False))
