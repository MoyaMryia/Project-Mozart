#!/usr/bin/env python3
"""Run an isolated, PyTorch-only RVC reference and save every model input."""

import argparse
import json
import os
import sys
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import torch


ROOT = Path(__file__).resolve().parent
RVC_SOURCE = Path(os.environ.get("RVC_SOURCE", "/home/moyamryia/mozart-archive/RVC"))
sys.path.insert(0, str(RVC_SOURCE))

from infer.hubert import HubertModelWithFinalProj  # noqa: E402
from infer.module.models import SynthesizerTrnMs768NSFsid  # noqa: E402
from infer.rmvpe import RMVPE  # noqa: E402
from infer.vc.pipeline import Pipeline  # noqa: E402


class ReferenceConfig:
    device = "cpu"
    is_half = False
    x_pad = 1
    x_query = 6
    x_center = 38
    x_max = 41


class CapturingGenerator(torch.nn.Module):
    def __init__(self, generator, tensor_dir):
        super().__init__()
        self.generator = generator
        self.tensor_dir = tensor_dir
        self.call = 0

    def infer(self, feats, p_len, pitch, pitchf, sid):
        prefix = self.tensor_dir / f"generator_{self.call:02d}"
        np.save(prefix.with_name(prefix.name + "_feats.npy"), feats.detach().cpu().numpy())
        np.save(prefix.with_name(prefix.name + "_p_len.npy"), p_len.detach().cpu().numpy())
        np.save(prefix.with_name(prefix.name + "_pitch.npy"), pitch.detach().cpu().numpy())
        np.save(prefix.with_name(prefix.name + "_pitchf.npy"), pitchf.detach().cpu().numpy())
        np.save(prefix.with_name(prefix.name + "_sid.npy"), sid.detach().cpu().numpy())
        # Match tools/export_generator_onnx.py: avoid the random VAE sample in
        # infer() so PyTorch and ONNX receive the same deterministic function.
        g = self.generator.emb_g(sid).unsqueeze(-1)
        m_p, _, x_mask = self.generator.enc_p(feats, pitch, p_len)
        z = self.generator.flow(m_p * x_mask, x_mask, g=g, reverse=True)
        audio = self.generator.dec(z * x_mask, pitchf, g=g)
        result = (audio, x_mask, (z, m_p))
        np.save(prefix.with_name(prefix.name + "_audio.npy"), result[0].detach().cpu().numpy())
        self.call += 1
        return result


def load_generator(model_path):
    checkpoint = torch.load(model_path, map_location="cpu", weights_only=False)
    if checkpoint.get("version") != "v2" or checkpoint.get("f0", 1) != 1:
        raise ValueError("reference runner requires an RVC v2 F0 model")
    checkpoint["config"][-3] = checkpoint["weight"]["emb_g.weight"].shape[0]
    generator = SynthesizerTrnMs768NSFsid(*checkpoint["config"], is_half=False)
    generator.load_state_dict(checkpoint["weight"], strict=False)
    # Load weight-norm parameters before removing the parametrizations.
    del generator.enc_q
    generator.eval().float()
    generator.remove_weight_norm()
    generator.dec.m_source.l_sin_gen.noise_std = 0.0
    return generator, int(checkpoint["config"][-1])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "input" / "sample-10s.wav"))
    parser.add_argument("--model", default="/home/moyamryia/models/de_narrator_clean.pth")
    parser.add_argument("--output", default=str(ROOT / "output" / "python-reference.wav"))
    parser.add_argument("--tensor-dir", default=str(ROOT / "tensors"))
    args = parser.parse_args()

    torch.manual_seed(114514)
    np.random.seed(114514)

    tensor_dir = Path(args.tensor_dir)
    tensor_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    audio, source_rate = sf.read(args.input, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if source_rate != 16000:
        audio = librosa.resample(audio, orig_sr=source_rate, target_sr=16000)
    audio = np.ascontiguousarray(audio, dtype=np.float32)
    audio_max = np.abs(audio).max() / 0.95
    if audio_max > 1:
        audio /= audio_max
    np.save(tensor_dir / "input_16k.npy", audio)

    generator, target_rate = load_generator(args.model)
    capturing_generator = CapturingGenerator(generator, tensor_dir)

    hubert_path = ROOT / "assets" / "hubert_base"
    hubert = HubertModelWithFinalProj.from_pretrained(
        str(hubert_path), local_files_only=True, torch_dtype=torch.float32
    ).eval().float()

    rmvpe_path = ROOT / "assets" / "rmvpe" / "rmvpe.pt"
    pipeline = Pipeline(target_rate, ReferenceConfig())
    pipeline.model_rmvpe = RMVPE(str(rmvpe_path), is_half=False, device="cpu")

    original_extract = pipeline.model_rmvpe.extract_mel
    original_decode = pipeline.model_rmvpe.decode

    def capture_mel(samples, center=True):
        mel = original_extract(samples, center=center)
        np.save(tensor_dir / "rmvpe_mel.npy", mel.detach().cpu().numpy())
        return mel

    def capture_f0(hidden, thred=0.03):
        np.save(tensor_dir / "rmvpe_salience.npy", np.asarray(hidden))
        f0 = original_decode(hidden, thred=thred)
        np.save(tensor_dir / "rmvpe_f0.npy", f0)
        return f0

    pipeline.model_rmvpe.extract_mel = capture_mel
    pipeline.model_rmvpe.decode = capture_f0

    times = [0.0, 0.0, 0.0]
    with torch.inference_mode():
        converted = pipeline.pipeline(
            hubert,
            capturing_generator,
            0,
            audio,
            times,
            0,
            "rmvpe",
            "",
            0.0,
            1,
            target_rate,
            target_rate,
            1.0,
            "v2",
            0.33,
        )

    sf.write(output_path, converted.astype(np.float32) / 32768.0, target_rate, subtype="FLOAT")
    metadata = {
        "input": str(Path(args.input).resolve()),
        "model": str(Path(args.model).resolve()),
        "output": str(output_path.resolve()),
        "source_rate": source_rate,
        "target_rate": target_rate,
        "input_samples_16k": int(audio.size),
        "output_samples": int(converted.size),
        "timings_seconds": times,
        "generator_calls": capturing_generator.call,
    }
    (ROOT / "reference.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
