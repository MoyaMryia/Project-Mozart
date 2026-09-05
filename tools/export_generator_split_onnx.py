#!/usr/bin/env python3
"""Export and validate bounded RVC Generator front and decoder graphs."""

import argparse
from pathlib import Path

import numpy as np
import torch

from export_generator_onnx import decode_generator, load_generator


class GeneratorFront(torch.nn.Module):
    def __init__(self, generator):
        super().__init__()
        self.generator = generator

    def forward(self, feats, p_len, pitch, sid, latent_noise):
        g = self.generator.emb_g(sid.long()).unsqueeze(-1)
        m_p, logs_p, x_mask = self.generator.enc_p(
            feats, pitch.long(), p_len.long()
        )
        z_p = (m_p + torch.exp(logs_p) * latent_noise * 0.66666) * x_mask
        return self.generator.flow(z_p, x_mask, g=g, reverse=True) * x_mask


class RealtimeGeneratorFront(torch.nn.Module):
    def __init__(self, generator, skip_head, return_frames):
        super().__init__()
        self.generator = generator
        self.flow_head = max(skip_head - 24, 0)
        self.decoder_head = skip_head - self.flow_head
        self.return_frames = return_frames

    def forward(self, feats, p_len, pitch, sid, latent_noise):
        g = self.generator.emb_g(sid.long()).unsqueeze(-1)
        m_p, logs_p, x_mask = self.generator.enc_p(
            feats, pitch.long(), p_len.long(), self.flow_head
        )
        z_p = (m_p + torch.exp(logs_p) * latent_noise * 0.66666) * x_mask
        z = self.generator.flow(z_p, x_mask, g=g, reverse=True) * x_mask
        begin = self.decoder_head
        return z[:, :, begin:begin + self.return_frames]


class GeneratorDecoder(torch.nn.Module):
    def __init__(self, generator):
        super().__init__()
        self.decoder = generator.dec
        self.emb_g = generator.emb_g

    def forward(self, z, pitchf, sid, source_phase, source_noise):
        g = self.emb_g(sid.long()).unsqueeze(-1)
        return decode_generator(
            self.decoder, z, pitchf, g, source_phase, source_noise
        )


def compare(name, expected, actual):
    delta = actual - expected
    rmse = float(np.sqrt(np.mean(delta * delta)))
    reference_rms = max(float(np.sqrt(np.mean(expected * expected))), 1e-12)
    relative = rmse / reference_rms
    correlation = float(np.corrcoef(actual.reshape(-1), expected.reshape(-1))[0, 1])
    print(
        f"{name}: shape={actual.shape} max_abs={np.max(np.abs(delta)):.9f} "
        f"relative_rmse={relative:.9%} correlation={correlation:.9f}"
    )
    if not np.isfinite(actual).all() or relative > 0.001 or correlation < 0.99999:
        raise RuntimeError(f"{name} mismatch")


def export_and_validate(
    module, inputs, path, input_names, output_name, opset, dynamo, test_data_dir
):
    module.eval()
    with torch.inference_mode():
        expected = module(*inputs).numpy()
    if test_data_dir:
        test_data_dir.mkdir(parents=True, exist_ok=True)
        prefix = path.stem
        for name, value in zip(input_names, inputs):
            value.numpy().tofile(test_data_dir / f"{prefix}-{name}.bin")
        np.save(test_data_dir / f"{prefix}-{output_name}.npy", expected)
    torch.onnx.export(
        module,
        inputs,
        path,
        input_names=input_names,
        output_names=[output_name],
        opset_version=opset,
        dynamo=dynamo,
    )

    import onnx
    import onnxruntime

    onnx.checker.check_model(path)
    session = onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"])
    feeds = {
        tensor.name: value.numpy()
        for tensor, value in zip(session.get_inputs(), inputs)
    }
    actual = session.run([output_name], feeds)[0]
    compare(path.name, expected, actual)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("--front-output", type=Path, required=True)
    parser.add_argument("--decoder-output", type=Path, required=True)
    parser.add_argument("--front-frames", type=int, default=1650)
    parser.add_argument("--decoder-frames", type=int, default=226)
    parser.add_argument(
        "--skip-head",
        type=int,
        help="fixed upstream-realtime history frames to remove before output",
    )
    parser.add_argument(
        "--return-frames",
        type=int,
        help="fixed upstream-realtime block + overlap + SOLA-search frames",
    )
    parser.add_argument("--opset", type=int, default=18)
    parser.add_argument("--dynamo-front", action="store_true")
    parser.add_argument("--test-data-dir", type=Path)
    parser.add_argument(
        "--rvc-source",
        type=Path,
        default=Path("/home/moyamryia/mozart-archive/RVC"),
    )
    args = parser.parse_args()
    if args.front_frames <= 0 or args.decoder_frames <= 0:
        raise ValueError("frame counts must be positive")
    if (args.skip_head is None) != (args.return_frames is None):
        raise ValueError("skip-head and return-frames must be specified together")
    realtime = args.skip_head is not None
    if realtime:
        if args.skip_head < 0 or args.return_frames <= 0:
            raise ValueError("invalid realtime skip/return frame counts")
        flow_head = max(args.skip_head - 24, 0)
        latent_frames = args.front_frames - flow_head
        decoder_head = args.skip_head - flow_head
        if decoder_head + args.return_frames > latent_frames:
            raise ValueError("realtime crop exceeds the post-encoder frame count")
        if args.decoder_frames != args.return_frames:
            raise ValueError("decoder-frames must equal realtime return-frames")
    else:
        latent_frames = args.front_frames

    wrapper, _ = load_generator(args.model, args.rvc_source)
    generator = wrapper.generator
    torch.manual_seed(114514)
    front_inputs = (
        torch.randn(1, args.front_frames, 768),
        torch.tensor([args.front_frames], dtype=torch.float32),
        torch.randint(1, 255, (1, args.front_frames), dtype=torch.float32),
        torch.tensor([0], dtype=torch.int64),
        torch.randn(1, generator.inter_channels, latent_frames),
    )
    source_dim = generator.dec.m_source.l_sin_gen.dim
    decoder_inputs = (
        torch.randn(1, generator.inter_channels, args.decoder_frames),
        torch.rand(1, args.decoder_frames) * 150.0 + 70.0,
        torch.tensor([0], dtype=torch.int64),
        torch.zeros(1, 1, source_dim),
        torch.randn(1, args.decoder_frames * generator.dec.upp, source_dim),
    )

    args.front_output.parent.mkdir(parents=True, exist_ok=True)
    args.decoder_output.parent.mkdir(parents=True, exist_ok=True)
    export_and_validate(
        RealtimeGeneratorFront(generator, args.skip_head, args.return_frames)
        if realtime else GeneratorFront(generator),
        front_inputs,
        args.front_output,
        ["feats", "p_len", "pitch", "sid", "latent_noise"],
        "z",
        args.opset,
        args.dynamo_front,
        args.test_data_dir,
    )
    export_and_validate(
        GeneratorDecoder(generator),
        decoder_inputs,
        args.decoder_output,
        ["z", "pitchf", "sid", "source_phase", "source_noise"],
        "audio",
        args.opset,
        False,
        args.test_data_dir,
    )


if __name__ == "__main__":
    main()
