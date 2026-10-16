#!/usr/bin/env python3
"""Compare deployed split ONNX assets with captured Golden tensors and PyTorch."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import onnxruntime as ort
import soundfile as sf
import torch


def metrics(expected, actual):
    if expected.shape != actual.shape or expected.dtype != actual.dtype:
        raise ValueError('Output shape or dtype differs')
    if not np.isfinite(expected).all() or not np.isfinite(actual).all():
        raise ValueError('Output contains non-finite samples')
    a, b = expected.astype(np.float64).ravel(), actual.astype(np.float64).ravel()
    rms = float(np.sqrt(np.mean(a*a)))
    relative = float(np.sqrt(np.mean((a-b)**2)) / max(rms, 1e-12))
    cosine = float(np.dot(a, b) / max(np.linalg.norm(a)*np.linalg.norm(b), 1e-12))
    result = dict(shape=list(actual.shape), dtype=str(actual.dtype), relative_rmse=relative,
                  cosine=cosine, max_abs=float(np.max(np.abs(a-b))), rms=float(np.sqrt(np.mean(b*b))))
    if relative > .001 or cosine < .99999:
        raise ValueError(f'Numerical comparison failed: {result}')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tensor-dir', type=Path, required=True)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--front', type=Path, required=True)
    parser.add_argument('--decoder', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--rvc-source', type=Path, required=True)
    parser.add_argument('--hubert', type=Path)
    parser.add_argument('--rmvpe', type=Path)
    parser.add_argument('--feature-assets', type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
    from export_generator_onnx import load_generator
    from export_generator_split_onnx import RealtimeGeneratorFront, GeneratorDecoder
    torch.set_num_threads(2)
    wrapper, rate = load_generator(args.checkpoint, args.rvc_source)
    generator = wrapper.generator
    def captured(name):
        return np.load(args.tensor_dir/f'generator_00_{name}.npy')
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    front = ort.InferenceSession(str(args.front), options, providers=['CPUExecutionProvider'])
    decoder = ort.InferenceSession(str(args.decoder), options, providers=['CPUExecutionProvider'])
    shapes = {item.name: item.shape for item in front.get_inputs()}
    frames, latent = shapes['feats'][1], shapes['latent_noise'][2]
    returned = next(item.shape[-1] for item in decoder.get_inputs() if item.name == 'z')
    skip = frames-returned
    source_dim = generator.dec.m_source.l_sin_gen.dim
    front_values = dict(feats=captured('feats')[:, -frames:, :].astype(np.float32),
        p_len=np.array([frames], np.float32), pitch=captured('pitch')[:, -frames:].astype(np.float32),
        sid=captured('sid').astype(np.int64), latent_noise=captured('latent_noise')[:, :, -latent:].astype(np.float32))
    def run_pt(module, values):
        with torch.inference_mode():
            return module(*(torch.from_numpy(value) for value in values.values())).numpy()
    expected_z = run_pt(RealtimeGeneratorFront(generator, skip, returned), front_values)
    onnx_z = front.run(['z'], front_values)[0]
    decoder_values = dict(z=expected_z, pitchf=captured('pitchf')[:, -returned:].astype(np.float32),
        sid=captured('sid').astype(np.int64), source_phase=captured('source_phase').astype(np.float32),
        source_noise=captured('source_noise')[:, -returned*generator.dec.upp:, :source_dim].astype(np.float32))
    expected_audio = run_pt(GeneratorDecoder(generator), decoder_values)
    onnx_audio = decoder.run(['audio'], decoder_values)[0]
    onnx_pipeline = decoder.run(['audio'], {**decoder_values, 'z': onnx_z})[0]
    report = dict(front=metrics(expected_z, onnx_z), decoder=metrics(expected_audio, onnx_audio),
                  pipeline=metrics(expected_audio, onnx_pipeline), sample_rate=rate,
                  output_seconds=onnx_pipeline.size/rate, input_origin='Captured offline Golden tail; fixed realtime crop')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if any([args.hubert, args.rmvpe, args.feature_assets]):
        if not all([args.hubert, args.rmvpe, args.feature_assets]):
            parser.error('Feature validation requires --hubert, --rmvpe, and --feature-assets')
        from transformers import HubertModel
        from export_hubert_onnx import RvcV2Hubert
        sys.path.insert(0, str(args.rvc_source))
        from infer.rmvpe import RMVPE
        source = np.load(args.tensor_dir/'input_16k.npy')[-44800:][None].astype(np.float32)
        hubert = RvcV2Hubert(HubertModel.from_pretrained(args.feature_assets/'hubert_base',
            local_files_only=True, torch_dtype=torch.float32).eval()).eval()
        expected_features = run_pt(hubert, {'audio': source})
        hubert_session = ort.InferenceSession(str(args.hubert), options, providers=['CPUExecutionProvider'])
        actual_features = hubert_session.run(['features'], {'audio': source})[0]
        report['hubert'] = metrics(expected_features, actual_features)
        mel = np.ascontiguousarray(np.load(args.tensor_dir/'rmvpe_mel.npy')[:, :, -32:], dtype=np.float32)
        rmvpe = RMVPE(str(args.feature_assets/'rmvpe/rmvpe.pt'), is_half=False, device='cpu')
        expected_salience = run_pt(rmvpe.model.eval(), {'mel': mel})
        rmvpe_session = ort.InferenceSession(str(args.rmvpe), options, providers=['CPUExecutionProvider'])
        actual_salience = rmvpe_session.run(['f0'], {'mel': mel})[0]
        report['rmvpe'] = metrics(expected_salience, actual_salience)
        for name, value in [('hubert-input', source), ('hubert-python', expected_features), ('hubert-onnx', actual_features),
                            ('rmvpe-input', mel), ('rmvpe-python', expected_salience), ('rmvpe-onnx', actual_salience)]:
            np.save(args.output_dir/f'{name}.npy', value)
    for prefix, values in [('front', front_values), ('decoder', decoder_values)]:
        for name, value in values.items():
            np.ascontiguousarray(value, dtype=np.float32).tofile(args.output_dir/f'{prefix}-{name}.bin')
    sf.write(args.output_dir/'qiqi-espeak-split-python.wav', expected_audio.ravel(), rate, subtype='FLOAT')
    sf.write(args.output_dir/'qiqi-espeak-split-onnx.wav', onnx_pipeline.ravel(), rate, subtype='FLOAT')
    report['assets'] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in [args.checkpoint, args.front, args.decoder]}
    if args.hubert:
        report['assets'].update({str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in [args.hubert, args.rmvpe]})
    (args.output_dir/'split-verification.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
