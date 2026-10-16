#!/usr/bin/env python3
"""用原始 Golden 张量验证动态导出，并准备 C++ CUDA 比较输入。"""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import onnxruntime as ort
import soundfile as sf
import torch

from verify_realtime_split import metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--rvc-source', type=Path, required=True)
    parser.add_argument('--tensor-dir', type=Path, required=True)
    parser.add_argument('--generator', type=Path, required=True)
    parser.add_argument('--rmvpe', type=Path, required=True)
    parser.add_argument('--feature-assets', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--generator-frames', type=int, nargs='+', default=[200,398])
    parser.add_argument('--rmvpe-frames', type=int, nargs='+', default=[32,128,1056])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
    from export_generator_onnx import load_generator
    torch.set_num_threads(2)
    wrapper, rate = load_generator(args.checkpoint, args.rvc_source)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    session = ort.InferenceSession(str(args.generator), options, providers=['CPUExecutionProvider'])
    report, cases = {}, []
    def captured(name):
        return np.load(args.tensor_dir/f'generator_00_{name}.npy')
    full = captured('feats').shape[1]
    def save_case(name, values, expected):
        inputs = []
        for key, value in values.items():
            path = f'{name}-{key}.bin'
            np.ascontiguousarray(value).tofile(args.output/path)
            inputs.append(dict(name=key, shape=list(value.shape), dtype=str(value.dtype), path=path))
        expected_path = name+'-expected.bin'
        expected.tofile(args.output/expected_path)
        return dict(name=name, inputs=inputs, expected=expected_path, elements=expected.size)
    for frames in dict.fromkeys([*args.generator_frames,full]):
        values = dict(feats=captured('feats')[:, :frames].astype(np.float32),
            p_len=np.array([frames], np.float32), pitch=captured('pitch')[:, :frames].astype(np.float32),
            pitchf=captured('pitchf')[:, :frames].astype(np.float32), sid=captured('sid').astype(np.int64),
            latent_noise=captured('latent_noise')[:, :, :frames].astype(np.float32),
            source_phase=captured('source_phase').astype(np.float32),
            source_noise=captured('source_noise')[:, :frames*(rate//100)].astype(np.float32))
        if frames == full:
            expected = captured('audio')
        else:
            with torch.inference_mode():
                expected = wrapper(*(torch.from_numpy(value) for value in values.values())).numpy()
        actual = session.run(['audio'], values)[0]
        report[f'generator-{frames}'] = metrics(expected, actual)
        cases.append(save_case(f'generator-{frames}', values, expected))
        if frames == full:
            crop = rate
            sf.write(args.output/'qiqi-espeak-dynamic-onnx-generator.wav', actual.ravel()[crop:-crop], rate, subtype='FLOAT')
    stages = [dict(name='generator', model=str(args.generator), output='audio', cases=cases)]
    del session, wrapper
    sys.path.insert(0, str(args.rvc_source))
    from infer.rmvpe import RMVPE
    rmvpe = RMVPE(str(args.feature_assets/'rmvpe/rmvpe.pt'), is_half=False, device='cpu')
    session = ort.InferenceSession(str(args.rmvpe), options, providers=['CPUExecutionProvider'])
    mel = np.load(args.tensor_dir/'rmvpe_mel.npy')
    cases = []
    for frames in args.rmvpe_frames:
        value = np.zeros((1, 128, frames), dtype=np.float32)
        count = min(frames, mel.shape[2])
        value[:, :, :count] = mel[:, :, :count]
        with torch.inference_mode():
            expected = rmvpe.model(torch.from_numpy(value)).numpy()
        actual = session.run(['f0'], {'mel': value})[0]
        report[f'rmvpe-{frames}'] = metrics(expected, actual)
        cases.append(save_case(f'rmvpe-{frames}', {'mel': value}, expected))
    stages.append(dict(name='rmvpe', model=str(args.rmvpe), output='f0', cases=cases))
    (args.output/'stages.json').write_text(json.dumps(stages, indent=2)+'\n')
    (args.output/'trt-stages.json').write_text(
        json.dumps([dict(**stage, backend='tensorrt') for stage in stages],indent=2)+'\n')
    (args.output/'python-verification.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
