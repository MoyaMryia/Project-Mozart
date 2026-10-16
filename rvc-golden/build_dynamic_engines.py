#!/usr/bin/env python3
"""构建有明确形状范围的文件推理 TensorRT 资产。"""
import argparse
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--feature-assets', type=Path, required=True)
    parser.add_argument('--max-generator-frames', type=int, default=2400)
    parser.add_argument('--max-rmvpe-frames', type=int, default=2304)
    parser.add_argument('--max-hubert-samples', type=int, default=384000)
    args = parser.parse_args()
    if args.max_generator_frames < 398 or args.max_rmvpe_frames < 1056 or args.max_hubert_samples < 64000:
        parser.error('Maximum shapes must include the optimization shape')
    if args.max_rmvpe_frames % 32:
        parser.error('Maximum RMVPE time must be a multiple of 32')
    args.assets.mkdir(parents=True, exist_ok=True)
    plans = []
    def generator(frames):
        return ','.join([f'feats:1x{frames}x768', 'p_len:1', f'pitch:1x{frames}', f'pitchf:1x{frames}',
                         'sid:1', f'latent_noise:1x192x{frames}', 'source_phase:1x1x1',
                         f'source_noise:1x{frames*480}x1'])
    for name, source, shapes in [
        ('qiqi-dynamic-explicit', args.assets/'qiqi-dynamic-explicit.onnx', [generator(n) for n in [100, 398, args.max_generator_frames]]),
        ('rmvpe-file', args.feature_assets/'rmvpe_dynamic.onnx', [f'mel:1x128x{n}' for n in [32, 1056, args.max_rmvpe_frames]]),
        ('hubert-file', args.feature_assets/'hubert_base_dynamic.onnx', [f'audio:1x{n}' for n in [32000, 64000, args.max_hubert_samples]])]:
        model = args.assets/(name+'.onnx')
        if source.resolve() != model.resolve() and not model.exists():
            model.symlink_to(source.resolve())
        command = ['/usr/bin/trtexec', '--onnx='+str(model), '--saveEngine='+str(model.with_suffix('.engine')),
            '--minShapes='+shapes[0], '--optShapes='+shapes[1], '--maxShapes='+shapes[2], '--noTF32',
            '--skipInference', '--memPoolSize=workspace:1024', '--builderOptimizationLevel=3', '--maxAuxStreams=0',
            '--profilingVerbosity=detailed']
        plans.append(dict(name=name, command=command))
        (args.assets/'build-plans.json').write_text(json.dumps(plans, indent=2)+'\n')
        with (args.assets/(name+'-build.log')).open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=900)
        print(name, 'completed', flush=True)


if __name__ == '__main__':
    main()
