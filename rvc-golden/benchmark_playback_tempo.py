#!/usr/bin/env python3
"""对照原速和保音高变速，保存实际输出及识别结果。"""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

import numpy as np
import soundfile as sf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pitch-only', action='store_true')
    args = parser.parse_args()
    if args.pitch_only:
        import sys
        import torch
        from scipy.signal import resample_poly
        sys.path.insert(0, str(Path.home()/'mozart-archive/RVC'))
        from infer.rmvpe import RMVPE
        torch.set_num_threads(2)
        model = RMVPE(str(Path.home()/'Mozart/rvc-golden/assets/rmvpe/rmvpe.pt'), is_half=False, device='cpu')
        records = json.loads((args.output/'quality.json').read_text())
        baseline = {}
        for row in records:
            audio, rate = sf.read(row['output'], dtype='float32')
            div = int(np.gcd(rate, 16000))
            pitch = model.infer_from_audio(resample_poly(audio, 16000//div, rate//div), thred=.03)
            if row['tempo'] == 1:
                baseline[row['name']] = pitch
            ref = baseline[row['name']]
            # 按实际采样长度对齐变速前后的时间轴，避免把缩短时长误算成音高偏移。
            positions = np.minimum(len(ref)-1, np.rint(np.arange(len(pitch))*row['tempo']).astype(int))
            ref = ref[positions]
            both, either = (ref>0)&(pitch>0), (ref>0)|(pitch>0)
            cents = abs(1200*np.log2(pitch[both]/ref[both]))
            row.update(f0_method='official PyTorch RMVPE, CPU, 16 kHz, threshold 0.03',
                f0_median_cents=float(np.median(cents)) if len(cents) else None,
                f0_p90_cents=float(np.percentile(cents,90)) if len(cents) else None,
                voiced_iou=float(np.count_nonzero(both)/max(1,np.count_nonzero(either))))
        (args.output/'quality-rmvpe.json').write_text(json.dumps(records, ensure_ascii=False, indent=2)+'\n')
        return
    import sherpa_onnx as so
    model = Path.home()/'models/sherpa-onnx/clone-test-downloads/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'
    recognizer = so.OfflineRecognizer.from_sense_voice(model=str(model/'model.int8.onnx'),
        tokens=str(model/'tokens.txt'), num_threads=2, language='en', use_itn=True)
    args.output.mkdir(parents=True, exist_ok=False)
    records = []
    references = {}
    for tempo in (1., 1.05, 1.08, 1.1):
        for path in sorted(args.input_dir.glob('00-*.wav')):
            if not re.fullmatch(r'00-\d{2}\.wav', path.name):
                continue
            output = args.output/f'{path.stem}-tempo-{tempo:.2f}.wav'
            began = time.monotonic()
            if tempo == 1:
                shutil.copyfile(path, output)
            else:
                subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(path),
                    '-af', f'atempo={tempo}', '-c:a', 'pcm_s16le', str(output)], check=True)
            elapsed = time.monotonic()-began
            audio, rate = sf.read(output, dtype='float32')
            stream = recognizer.create_stream()
            samples = np.interp(np.arange(0,len(audio),rate/16000),np.arange(len(audio)),audio).astype(np.float32)
            stream.accept_waveform(16000, samples)
            recognizer.decode_stream(stream)
            text = stream.result.text.strip()
            if tempo == 1:
                references[path.name] = text
            ref = references[path.name]
            row = dict(name=path.name, input=str(path), output=str(output), tempo=tempo,
                seconds=len(audio)/rate, rate=rate, processing_seconds=elapsed,
                transcript=text, reference_transcript=ref,
                same_transcript=re.sub(r'\W','',text.lower())==re.sub(r'\W','',ref.lower()),
                rms=float(np.sqrt(np.mean(audio**2))), peak=float(np.max(abs(audio))),
                fullscale_samples=int(np.count_nonzero(abs(audio)>=32767/32768)))
            records.append(row)
            (args.output/'quality.json').write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps(row), flush=True)


if __name__ == '__main__':
    main()
