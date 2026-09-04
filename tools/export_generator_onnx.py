#!/usr/bin/env python3
"""Export an RVC v2 F0 generator with explicit synthesis noise inputs."""

import argparse
import sys
import types
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F


class GeneratorWrapper(torch.nn.Module):
    def __init__(self, generator):
        super().__init__()
        self.generator = generator

    def _decode(self, z, pitchf, g, source_phase, source_noise):
        decoder = self.generator.dec
        sine = decoder.m_source.l_sin_gen

        f0 = pitchf[:, None].transpose(1, 2)
        rad = f0 / sine.sampling_rate * torch.arange(
            1, decoder.upp + 1, device=f0.device
        )
        rad2 = torch.fmod(rad[..., -1:].float() + 0.5, 1.0) - 0.5
        rad_acc = rad2.cumsum(dim=1).fmod(1.0).to(f0)
        rad += F.pad(rad_acc, (0, 0, 1, -1))
        rad = rad.reshape(f0.shape[0], -1, 1)
        rad = rad * torch.arange(
            1, sine.dim + 1, device=f0.device
        ).reshape(1, 1, -1)
        sine_waves = torch.sin(2 * np.pi * (rad + source_phase)) * sine.sine_amp
        uv = (f0 > sine.voiced_threshold).to(f0)
        uv = F.interpolate(
            uv.transpose(2, 1), scale_factor=float(decoder.upp), mode="nearest"
        ).transpose(2, 1)
        noise_amp = uv * sine.noise_std + (1 - uv) * sine.sine_amp / 3
        source = sine_waves * uv + noise_amp * source_noise
        source = decoder.m_source.l_tanh(decoder.m_source.l_linear(
            source.to(dtype=decoder.m_source.l_linear.weight.dtype)
        )).transpose(1, 2)

        x = decoder.conv_pre(z)
        x = x + decoder.cond(g)
        for i, (upsample, noise_conv) in enumerate(
            zip(decoder.ups, decoder.noise_convs)
        ):
            x = F.leaky_relu(x, decoder.lrelu_slope)
            x = upsample(x)
            x = x + noise_conv(source)
            combined = None
            first = i * decoder.num_kernels
            for resblock in decoder.resblocks[first:first + decoder.num_kernels]:
                value = resblock(x)
                combined = value if combined is None else combined + value
            x = combined / decoder.num_kernels
        x = F.leaky_relu(x)
        return torch.tanh(decoder.conv_post(x))

    def forward(
        self, feats, p_len, pitch, pitchf, sid,
        latent_noise, source_phase, source_noise,
    ):
        g = self.generator.emb_g(sid.long()).unsqueeze(-1)
        m_p, logs_p, x_mask = self.generator.enc_p(
            feats, pitch.long(), p_len.long()
        )
        z_p = (
            m_p + torch.exp(logs_p) * latent_noise * 0.66666
        ) * x_mask
        z = self.generator.flow(z_p, x_mask, g=g, reverse=True)
        return self._decode(z * x_mask, pitchf, g, source_phase, source_noise)


def enable_dynamic_attention(generator):
    """Replace trace-time integer reshapes with equivalent symbolic forms."""
    def get_relative_embeddings(self, embeddings, length):
        pad_length = length - (self.window_size + 1)
        padded = F.pad(
            embeddings, [0, 0, pad_length, pad_length, 0, 0]
        )
        return padded[:, :2 * length - 1]

    def relative_to_absolute(self, x):
        batch, heads, length, _ = x.size()
        x = F.pad(x, [0, 1, 0, 0, 0, 0, 0, 0])
        x = x.reshape(batch, heads, -1)
        x = F.pad(x, [0, length - 1, 0, 0, 0, 0])
        return x.reshape(batch, heads, length + 1, 2 * length - 1)[
            :, :, :length, length - 1:
        ]

    def absolute_to_relative(self, x):
        batch, heads, length, _ = x.size()
        x = F.pad(x, [0, length - 1, 0, 0, 0, 0, 0, 0])
        x = x.reshape(batch, heads, -1)
        x = F.pad(x, [length, 0, 0, 0, 0, 0])
        return x.reshape(batch, heads, length, 2 * length)[:, :, :, 1:]

    for module in generator.modules():
        if not hasattr(module, "window_size") or module.window_size is None:
            continue
        module._get_relative_embeddings = types.MethodType(
            get_relative_embeddings, module
        )
        module._relative_position_to_absolute_position = types.MethodType(
            relative_to_absolute, module
        )
        module._absolute_position_to_relative_position = types.MethodType(
            absolute_to_relative, module
        )


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
    return GeneratorWrapper(generator.eval().float()).eval(), int(config[-1])


def export_model(
    model_path, output_path, rvc_source, opset, frames,
    dynamo=False, dynamic_time=False,
):
    wrapper, sample_rate = load_generator(model_path, rvc_source)
    if dynamic_time:
        enable_dynamic_attention(wrapper.generator)
    torch.manual_seed(114514)

    def make_inputs(length):
        generator = wrapper.generator
        source_dim = generator.dec.m_source.l_sin_gen.dim
        return (
            torch.randn(1, length, 768),
            torch.tensor([length], dtype=torch.float32),
            torch.randint(1, 255, (1, length), dtype=torch.float32),
            torch.rand(1, length) * 150.0 + 70.0,
            torch.tensor([0], dtype=torch.int64),
            torch.randn(1, generator.inter_channels, length),
            torch.zeros(1, 1, source_dim),
            torch.randn(1, length * generator.dec.upp, source_dim),
        )

    inputs = make_inputs(frames)

    torch.onnx.export(
        wrapper,
        inputs,
        output_path,
        input_names=[
            "feats", "p_len", "pitch", "pitchf", "sid",
            "latent_noise", "source_phase", "source_noise",
        ],
        output_names=["audio"],
        opset_version=opset,
        dynamo=dynamo,
        dynamic_axes={
            "feats": {1: "frames"},
            "pitch": {1: "frames"},
            "pitchf": {1: "frames"},
            "latent_noise": {2: "frames"},
            "source_noise": {1: "audio_samples"},
            "audio": {2: "audio_samples"},
        } if dynamic_time else None,
    )

    import onnx
    import onnxruntime

    onnx.checker.check_model(output_path)
    session = onnxruntime.InferenceSession(output_path, providers=["CPUExecutionProvider"])
    test_lengths = [frames]
    if dynamic_time:
        test_lengths = sorted({max(8, frames // 2), frames, frames + 2})
    for length in test_lengths:
        test_inputs = make_inputs(length)
        with torch.inference_mode():
            expected = wrapper(*test_inputs).numpy()
        feeds = {
            tensor.name: value.numpy()
            for tensor, value in zip(session.get_inputs(), test_inputs)
        }
        audio = session.run(["audio"], feeds)[0]
        expected_samples = length * sample_rate // 100
        if audio.shape != (1, 1, expected_samples):
            raise RuntimeError(f"unexpected generator output shape: {audio.shape}")
        if not np.isfinite(audio).all():
            raise RuntimeError("generator output contains non-finite values")
        delta = audio - expected
        rmse = float(np.sqrt(np.mean(delta * delta)))
        relative = rmse / max(float(np.sqrt(np.mean(expected * expected))), 1e-12)
        correlation = float(np.corrcoef(audio.reshape(-1), expected.reshape(-1))[0, 1])
        print(
            f"T={length}: max_abs={np.max(np.abs(delta)):.6f} "
            f"rmse={rmse:.6f} relative_rmse={relative:.6%} "
            f"correlation={correlation:.6f}"
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
        "--dynamic-time",
        action="store_true",
        help="Export symbolic Generator frame and output-sample axes",
    )
    parser.add_argument(
        "--dynamo",
        action="store_true",
        help="Use the dynamo exporter; fake-tensor propagation avoids the "
        "activation memory blow-up of legacy tracing at large frame counts",
    )
    args = parser.parse_args()

    output = args.output or args.model.with_suffix(".onnx")
    output.parent.mkdir(parents=True, exist_ok=True)
    export_model(
        args.model, output, args.rvc_source, args.opset, args.frames,
        args.dynamo, args.dynamic_time,
    )


if __name__ == "__main__":
    main()
