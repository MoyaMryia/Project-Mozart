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
        # RVC's reference infer() samples Gaussian noise for z_p. That makes
        # PyTorch and ONNX use different random streams and can produce a
        # completely different waveform from identical tensors. Voice
        # conversion does not need that VAE sampling, so use its deterministic
        # mean path for a reproducible cross-runtime contract.
        g = self.generator.emb_g(sid.long()).unsqueeze(-1)
        m_p, _, x_mask = self.generator.enc_p(feats, pitch.long(), p_len.long())
        z = self.generator.flow(m_p * x_mask, x_mask, g=g, reverse=True)
        return self.generator.dec(z * x_mask, pitchf, g=g)


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
    generator.dec.m_source.l_sin_gen.noise_std = 0.0
    return GeneratorWrapper(generator.eval().float()), int(config[-1])


def export_model(model_path, output_path, rvc_source, opset, frames, dynamo=False):
    wrapper, sample_rate = load_generator(model_path, rvc_source)
    torch.manual_seed(114514)

    def make_inputs(length):
        return (
            torch.randn(1, length, 768),
            torch.tensor([length], dtype=torch.float32),
            torch.randint(1, 255, (1, length), dtype=torch.float32),
            torch.rand(1, length) * 150.0 + 70.0,
            torch.tensor([0], dtype=torch.int64),
        )

    inputs = make_inputs(frames)

    torch.onnx.export(
        wrapper,
        inputs,
        output_path,
        input_names=["feats", "p_len", "pitch", "pitchf", "sid"],
        output_names=["audio"],
        opset_version=opset,
        dynamo=dynamo,
    )

    import onnx
    import onnxruntime

    onnx.checker.check_model(output_path)
    session = onnxruntime.InferenceSession(output_path, providers=["CPUExecutionProvider"])
    test_inputs = make_inputs(frames)
    with torch.inference_mode():
        expected = wrapper(*test_inputs).numpy()
    feeds = {
        tensor.name: value.numpy()
        for tensor, value in zip(session.get_inputs(), test_inputs)
    }
    audio = session.run(["audio"], feeds)[0]
    expected_samples = frames * sample_rate // 100
    if audio.shape != (1, 1, expected_samples):
        raise RuntimeError(f"unexpected generator output shape: {audio.shape}")
    if not np.isfinite(audio).all():
        raise RuntimeError("generator output contains non-finite values")
    delta = audio - expected
    rmse = float(np.sqrt(np.mean(delta * delta)))
    relative = rmse / max(float(np.sqrt(np.mean(expected * expected))), 1e-12)
    correlation = float(np.corrcoef(audio.reshape(-1), expected.reshape(-1))[0, 1])
    print(
        f"T={frames}: max_abs={np.max(np.abs(delta)):.6f} "
        f"rmse={rmse:.6f} relative_rmse={relative:.6%} correlation={correlation:.6f}"
    )
    if relative > 0.03 or correlation < 0.999:
        raise RuntimeError("PyTorch/ONNX mismatch")


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
    parser.add_argument("--frames", type=int, default=200)
    parser.add_argument(
        "--dynamo",
        action="store_true",
        help="Use the dynamo exporter; fake-tensor propagation avoids the "
        "activation memory blow-up of legacy tracing at large frame counts",
    )
    args = parser.parse_args()

    output = args.output or args.model.with_suffix(".onnx")
    output.parent.mkdir(parents=True, exist_ok=True)
    export_model(args.model, output, args.rvc_source, args.opset, args.frames, args.dynamo)


if __name__ == "__main__":
    main()
