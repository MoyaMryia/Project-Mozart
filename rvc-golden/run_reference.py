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
PROJECT_ROOT = ROOT.parent
RVC_SOURCE = Path(os.environ.get("RVC_SOURCE", "/home/moyamryia/mozart-archive/RVC"))
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(RVC_SOURCE))

from infer.hubert import HubertModelWithFinalProj  # noqa: E402
from infer.module.models import SynthesizerTrnMs768NSFsid  # noqa: E402
from infer.rmvpe import RMVPE  # noqa: E402
from infer.vc.pipeline import Pipeline  # noqa: E402
from tools.export_generator_onnx import GeneratorWrapper  # noqa: E402


class ReferenceConfig:
    device = "cpu"
    is_half = False
    x_pad = 1
    x_query = 6
    x_center = 38
    x_max = 41


def set_wav_peak_timestamp(path, timestamp):
    """Set libsndfile's PEAK timestamp so a FLOAT WAV is byte-reproducible."""
    if not 0 <= timestamp <= 0xFFFFFFFF:
        raise ValueError("WAV PEAK timestamp must fit in an unsigned 32-bit integer")
    with path.open("r+b") as wav:
        header = wav.read(12)
        if header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise ValueError(f"not a RIFF/WAVE file: {path}")
        while chunk_header := wav.read(8):
            if len(chunk_header) != 8:
                break
            chunk_id = chunk_header[:4]
            chunk_size = int.from_bytes(chunk_header[4:], "little")
            if chunk_id == b"PEAK":
                if chunk_size < 8:
                    break
                wav.seek(4, 1)
                wav.write(timestamp.to_bytes(4, "little"))
                return
            wav.seek(chunk_size + (chunk_size & 1), 1)
    raise ValueError(f"FLOAT WAV has no valid PEAK chunk: {path}")


class CapturingGenerator(torch.nn.Module):
    def __init__(self, generator, tensor_dir, deterministic=False):
        super().__init__()
        self.generator = generator
        self.tensor_dir = tensor_dir
        self.call = 0
        self.deterministic = deterministic
        self.export_wrapper = GeneratorWrapper(generator)

    def infer(self, feats, p_len, pitch, pitchf, sid):
        prefix = self.tensor_dir / f"generator_{self.call:02d}"
        np.save(prefix.with_name(prefix.name + "_feats.npy"), feats.detach().cpu().numpy())
        np.save(prefix.with_name(prefix.name + "_p_len.npy"), p_len.detach().cpu().numpy())
        np.save(prefix.with_name(prefix.name + "_pitch.npy"), pitch.detach().cpu().numpy())
        np.save(prefix.with_name(prefix.name + "_pitchf.npy"), pitchf.detach().cpu().numpy())
        np.save(prefix.with_name(prefix.name + "_sid.npy"), sid.detach().cpu().numpy())
        if self.deterministic:
            # Optional ONNX-alignment path. This is not the original RVC
            # inference path and must not be the Golden default.
            g = self.generator.emb_g(sid).unsqueeze(-1)
            m_p, _, x_mask = self.generator.enc_p(feats, pitch, p_len)
            z = self.generator.flow(m_p * x_mask, x_mask, g=g, reverse=True)
            audio = self.generator.dec(z * x_mask, pitchf, g=g)
            result = (audio, x_mask, (z, m_p))
        else:
            latent_noise = torch.randn(
                feats.shape[0], self.generator.inter_channels, feats.shape[1],
                device=feats.device, dtype=feats.dtype,
            )
            source_dim = self.generator.dec.m_source.l_sin_gen.dim
            source_phase = torch.rand(
                1, 1, source_dim, device=feats.device, dtype=feats.dtype
            )
            source_phase[..., 0] = 0
            source_noise = torch.randn(
                feats.shape[0], pitchf.shape[1] * self.generator.dec.upp, source_dim,
                device=feats.device, dtype=feats.dtype,
            )
            np.save(
                prefix.with_name(prefix.name + "_latent_noise.npy"),
                latent_noise.detach().cpu().numpy(),
            )
            np.save(
                prefix.with_name(prefix.name + "_source_phase.npy"),
                source_phase.detach().cpu().numpy(),
            )
            np.save(
                prefix.with_name(prefix.name + "_source_noise.npy"),
                source_noise.detach().cpu().numpy(),
            )
            audio = self.export_wrapper(
                feats, p_len, pitch, pitchf, sid,
                latent_noise, source_phase, source_noise,
            )
            result = (audio, None, None)
        np.save(prefix.with_name(prefix.name + "_audio.npy"), result[0].detach().cpu().numpy())
        self.call += 1
        return result


def load_generator(model_path, deterministic=False):
    checkpoint = torch.load(model_path, map_location="cpu", weights_only=False)
    if checkpoint.get("version") != "v2" or checkpoint.get("f0", 1) != 1:
        raise ValueError("reference runner requires an RVC v2 F0 model")
    checkpoint["config"][-3] = checkpoint["weight"]["emb_g.weight"].shape[0]
    generator = SynthesizerTrnMs768NSFsid(*checkpoint["config"], is_half=False)
    incompatible = generator.load_state_dict(checkpoint["weight"], strict=False)
    missing_inference_weights = [
        key for key in incompatible.missing_keys if not key.startswith("enc_q.")
    ]
    if missing_inference_weights or incompatible.unexpected_keys:
        raise RuntimeError(
            "generator checkpoint mismatch: "
            f"missing={missing_inference_weights}, unexpected={incompatible.unexpected_keys}"
        )
    # Load weight-norm parameters before removing the parametrizations.
    del generator.enc_q
    generator.eval().float()
    generator.remove_weight_norm()
    if deterministic:
        generator.dec.m_source.l_sin_gen.noise_std = 0.0
    return generator, int(checkpoint["config"][-1])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "input" / "sample-10s.wav"))
    parser.add_argument("--model", default="/home/moyamryia/models/de_narrator_clean.pth")
    parser.add_argument("--output", default=str(ROOT / "output" / "python-reference.wav"))
    parser.add_argument("--tensor-dir", default=str(ROOT / "tensors"))
    parser.add_argument("--deterministic-generator", action="store_true",
                        help="use the export-alignment mean path instead of original RVC inference")
    parser.add_argument("--metadata", default=str(ROOT / "reference.json"),
                        help="path to write the run metadata (defaults to reference.json)")
    parser.add_argument("--seed", type=int, default=114514)
    parser.add_argument(
        "--wav-peak-timestamp",
        type=int,
        default=None,
        help="set the FLOAT WAV PEAK timestamp for byte-reproducible output",
    )
    args = parser.parse_args()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

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

    generator, target_rate = load_generator(
        args.model, deterministic=args.deterministic_generator
    )
    capturing_generator = CapturingGenerator(
        generator, tensor_dir, deterministic=args.deterministic_generator
    )

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
    if args.wav_peak_timestamp is not None:
        set_wav_peak_timestamp(output_path, args.wav_peak_timestamp)
    metadata = {
        "input": str(Path(args.input).resolve()),
        "model": str(Path(args.model).resolve()),
        "output": str(output_path.resolve()),
        "source_rate": source_rate,
        "target_rate": target_rate,
        "input_samples_16k": int(audio.size),
        "output_samples": int(converted.size),
        "seed": args.seed,
        "wav_peak_timestamp": args.wav_peak_timestamp,
        "timings_seconds": times,
        "generator_calls": capturing_generator.call,
    }
    metadata_path = Path(args.metadata)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
