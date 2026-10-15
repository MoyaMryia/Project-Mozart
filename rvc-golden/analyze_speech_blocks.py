#!/usr/bin/env python3
"""比较小音频块的文本、频谱、音高和块边界。"""
import argparse
import json
from pathlib import Path
import re

import numpy as np
import soundfile as sf


def features(audio, rate):
    frames = np.lib.stride_tricks.sliding_window_view(np.pad(audio,(512,512)),1024)[::240]
    window = frames*np.hanning(1024)
    spectrum = abs(np.fft.rfft(window))**2
    bins = np.fft.rfftfreq(1024, 1/rate)
    hz = 700*(10**(np.linspace(2595*np.log10(1+50/700),2595*np.log10(1+11000/700),50)/2595)-1)
    weights = np.maximum(0,np.minimum((bins[None]-hz[:-2,None])/(hz[1:-1,None]-hz[:-2,None]),
        (hz[2:,None]-bins[None])/(hz[2:,None]-hz[1:-1,None])))
    mel = np.log10(np.maximum(spectrum@weights.T,1e-10))
    return mel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--pitch-only', action='store_true')
    parser.add_argument('--rvc-source', type=Path)
    parser.add_argument('--rmvpe', type=Path)
    args = parser.parse_args()
    if args.pitch_only:
        import sys
        import torch
        from scipy.signal import resample_poly
        if not args.rvc_source or not args.rmvpe:
            parser.error('--pitch-only requires --rvc-source and --rmvpe')
        sys.path.insert(0, str(args.rvc_source))
        from infer.rmvpe import RMVPE
        torch.set_num_threads(2)
        model = RMVPE(str(args.rmvpe), is_half=False, device='cpu')
        quality = json.loads((args.root/'quality.json').read_text())
        baseline = {}
        for item in quality:
            records = json.loads((args.root/item['case']/'results.json').read_text())['records']
            row = next(r for r in records if r['index'] == item['index'] and not r['repeat'])
            audio, rate = sf.read(row['request']['output'], dtype='float32')
            divisor = np.gcd(rate, 16000)
            audio = resample_poly(audio, 16000//divisor, rate//divisor)
            pitch = model.infer_from_audio(audio, thred=.03)
            if item['case'] == 'baseline':
                baseline[item['index']] = pitch
            ref = baseline[item['index']]
            length = min(len(ref), len(pitch))
            ref, pitch = ref[:length], pitch[:length]
            both, either = (ref>0)&(pitch>0), (ref>0)|(pitch>0)
            cents = abs(1200*np.log2(pitch[both]/ref[both]))
            item.update(f0_method='official PyTorch RMVPE, CPU, 16 kHz, threshold 0.03',
                f0_median_cents=float(np.median(cents)) if len(cents) else None,
                f0_p90_cents=float(np.percentile(cents,90)) if len(cents) else None,
                voiced_iou=float(np.count_nonzero(both)/max(1,np.count_nonzero(either))))
        (args.root/'quality-rmvpe.json').write_text(json.dumps(quality,ensure_ascii=False,indent=2)+'\n')
        return
    import sherpa_onnx as so
    model = Path.home()/'models/sherpa-onnx/clone-test-downloads/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'
    recognizer = so.OfflineRecognizer.from_sense_voice(model=str(model/'model.int8.onnx'),
        tokens=str(model/'tokens.txt'), num_threads=2, language='en', use_itn=True)
    def transcript(audio,rate):
        stream = recognizer.create_stream()
        values = np.interp(np.arange(0,len(audio),rate/16000),np.arange(len(audio)),audio).astype(np.float32)
        stream.accept_waveform(16000,values)
        recognizer.decode_stream(stream)
        return stream.result.text.strip()
    baseline = {}
    result = []
    for item in json.loads((args.root/'summary.json').read_text()):
        records = json.loads((args.root/item['case']/'results.json').read_text())['records']
        for row in records:
            if row['repeat']:
                continue
            audio, rate = sf.read(row['request']['output'],dtype='float64')
            mel = features(audio,rate)
            text = transcript(audio,rate)
            if item['case']=='baseline':
                baseline[row['index']] = (audio,mel,text)
            original,ref_mel,ref_text = baseline[row['index']]
            differences = abs(np.diff(audio))
            period = round(item['frames']*.08*rate)
            edges = np.arange(period,len(audio),period)-1
            result.append(dict(case=item['case'],index=row['index'],text=text,baseline_text=ref_text,
                same_transcript=re.sub(r'\W','',text.lower())==re.sub(r'\W','',ref_text.lower()),
                logmel_correlation=float(np.corrcoef(mel.ravel(),ref_mel.ravel())[0,1]),
                max_boundary_jump=float(np.max(differences[edges])) if len(edges) else 0,
                baseline_jump_at_same_edges=float(np.max(abs(np.diff(original))[edges])) if len(edges) else 0,
                derivative_p999=float(np.percentile(differences,99.9)),
                samples=len(audio),peak=float(np.max(abs(audio)))))
    (args.root/'quality.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
