#!/usr/bin/env python3
"""Export an RVC v2 F0 generator for Mozart's fixed T=200 contract."""

import argparse
import sys
from pathlib import Path

import numpy as np
import torch


class GeneratorWrapper(torch.nn.Module):
    def __init__(self, generator):
        super().__init__()
        self.generator = generator

    def forward(self, feats, p_len, pitch, pitchf, sid):
        audio, _, _ = self.generator.infer(
            feats, p_len.long(), pitch.long(), pitchf, sid.long()
        )
        return audio


def load_generator(model_path, rvc_source):
    sys.path.insert(0, str(rvc_source))
    from infer.module.models import SynthesizerTrnMs768NSFsid

    checkpoint = torch.load(model_path, map_location="cpu", weights_only=False)
    if checkpoint.get("version") != "v2" or checkpoint.get("f0", 1) != 1:
        raise ValueError("Mozart requires an RVC v2 F0 checkpoint")

    config = list(checkpoint["config"])
    config[-3] = checkpoint["weight"]["emb_g.weight"].shape[0]
    generator = SynthesizerTrnMs768NSFsid(*config, is_half=False)

    incompatible = generator.load_state_dict(checkpoint["weight"], strict=False)
    missing = [name for name in incompatible.missing_keys if not name.startswith("enc_q.")]
    unexpected = [name for name in incompatible.unexpected_keys if not name.startswith("enc_q.")]
    if missing or unexpected:
        raise RuntimeError(
            f"checkpoint mismatch: missing={missing[:10]}, unexpected={unexpected[:10]}"
        )

    # Checkpoints store convolution parameters as weight_g/weight_v. They must
    # be loaded before weight norm is removed.
    generator.remove_weight_norm()
    if hasattr(generator, "enc_q"):
        del generator.enc_q
    return GeneratorWrapper(generator.eval().float()), int(config[-1])


def export_model(model_path, output_path, rvc_source, opset):
    wrapper, sample_rate = load_generator(model_path, rvc_source)
    frames = 200
    inputs = (
        torch.randn(1, frames, 768),
        torch.tensor([frames], dtype=torch.float32),
        torch.randint(1, 255, (1, frames), dtype=torch.float32),
        torch.rand(1, frames) * 150.0 + 70.0,
        torch.tensor([0], dtype=torch.int64),
    )

    torch.onnx.export(
        wrapper,
        inputs,
        output_path,
        input_names=["feats", "p_len", "pitch", "pitchf", "sid"],
        output_names=["audio"],
        opset_version=opset,
        dynamo=False,
    )

    import onnx
    import onnxruntime

    onnx.checker.check_model(output_path)
    session = onnxruntime.InferenceSession(output_path, providers=["CPUExecutionProvider"])
    feeds = {
        tensor.name: value.numpy()
        for tensor, value in zip(session.get_inputs(), inputs)
    }
    audio = session.run(["audio"], feeds)[0]
    expected_samples = frames * sample_rate // 100
    if audio.shape != (1, 1, expected_samples):
        raise RuntimeError(f"unexpected generator output shape: {audio.shape}")
    if not np.isfinite(audio).all():
        raise RuntimeError("generator output contains non-finite values")
    print(f"Exported {output_path}: shape={audio.shape}, rms={np.sqrt(np.mean(audio**2)):.6f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("model", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--rvc-source",
        type=Path,
        default=Path("/home/moyamryia/mozart-archive/RVC"),
    )
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    output = args.output or args.model.with_suffix(".onnx")
    output.parent.mkdir(parents=True, exist_ok=True)
    export_model(args.model, output, args.rvc_source, args.opset)


if __name__ == "__main__":
    main()
