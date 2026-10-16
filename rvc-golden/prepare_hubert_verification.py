#!/usr/bin/env python3
"""核对整段 HuBERT 与 Golden 特征，并补充 TensorRT 输入。"""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import onnxruntime as ort
from scipy import signal
import torch
from transformers import HubertModel
from verify_realtime_split import metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--tensor-dir', type=Path, required=True)
    parser.add_argument('--feature-assets', type=Path, required=True)
    parser.add_argument('--reuse-manifest', type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
    from export_hubert_onnx import RvcV2Hubert
    torch.set_num_threads(2)
    model = RvcV2Hubert(HubertModel.from_pretrained(args.feature_assets/'hubert_base',
        local_files_only=True, torch_dtype=torch.float32).eval()).eval()
    audio = np.load(args.tensor_dir/'input_16k.npy')
    b, a = signal.butter(5, 48, btype='high', fs=16000)
    padded = np.pad(signal.filtfilt(b, a, audio), (16000,16000), mode='reflect').astype(np.float32)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    session = ort.InferenceSession(str(args.work/'assets/hubert-file.onnx'), options, providers=['CPUExecutionProvider'])
    report, cases = {}, []
    output = args.work/'verification'
    for samples in [32000,64000,len(padded)]:
        value = padded[:samples][None]
        with torch.inference_mode():
            expected = model(torch.from_numpy(value)).numpy()
        actual = session.run(['features'], {'audio': value})[0]
        report[str(samples)] = metrics(expected, actual)
        if samples == len(padded):
            captured = np.load(args.tensor_dir/'generator_00_feats.npy')
            report['golden_features'] = metrics(captured, np.repeat(expected, 2, axis=1))
        name = f'hubert-{samples}'
        value.tofile(output/(name+'-audio.bin'))
        expected.tofile(output/(name+'-expected.bin'))
        cases.append(dict(name=name, inputs=[dict(name='audio', shape=list(value.shape), dtype='float32',
            path=name+'-audio.bin')], expected=name+'-expected.bin', elements=expected.size))
    path = output/'trt-stages.json'
    stages = [item for item in json.loads(path.read_text()) if item['name'] != 'hubert']
    stages.append(dict(name='hubert', model=str(args.work/'assets/hubert-file.onnx'),
                       backend='tensorrt', output='features', cases=cases))
    for stage in stages:
        model_name = {'generator':'qiqi-dynamic-explicit', 'rmvpe':'rmvpe-file', 'hubert':'hubert-file'}[stage['name']]
        stage['model'] = str(args.work/'assets'/(model_name+'.onnx'))
        stage['backend'] = 'tensorrt'
    if args.reuse_manifest:
        for previous in json.loads(args.reuse_manifest.read_text()):
            stage = next(item for item in stages if item['name'] == previous['name'])
            names = {item['name'] for item in stage['cases']}
            for item in previous['cases']:
                if item['name'] in names:
                    continue
                item['expected'] = str((args.reuse_manifest.parent/item['expected']).resolve())
                for value in item['inputs']:
                    value['path'] = str((args.reuse_manifest.parent/value['path']).resolve())
                stage['cases'].append(item)
    path.write_text(json.dumps(stages, indent=2)+'\n')
    (output/'hubert-python-verification.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
