#!/usr/bin/env python3
"""Run the exported ONNX generator with exact inputs captured from PyTorch."""

import argparse
from pathlib import Path

import numpy as np
import onnxruntime as ort
import soundfile as sf


ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        default="/home/moyamryia/Mozart/rvc-backend/models/de_narrator/generator_dynamic.onnx",
    )
    parser.add_argument("--output", default=str(ROOT / "output" / "onnx-golden-input.wav"))
    args = parser.parse_args()

    prefix = ROOT / "tensors" / "generator_00"
    feeds = {
        "feats": np.load(prefix.with_name(prefix.name + "_feats.npy")),
        "p_len": np.load(prefix.with_name(prefix.name + "_p_len.npy")).astype(np.float32),
        "pitch": np.load(prefix.with_name(prefix.name + "_pitch.npy")).astype(np.float32),
        "pitchf": np.load(prefix.with_name(prefix.name + "_pitchf.npy")).astype(np.float32),
        "sid": np.load(prefix.with_name(prefix.name + "_sid.npy")).astype(np.int64),
        "latent_noise": np.load(
            prefix.with_name(prefix.name + "_latent_noise.npy")
        ).astype(np.float32),
        "source_phase": np.load(
            prefix.with_name(prefix.name + "_source_phase.npy")
        ).astype(np.float32),
        "source_noise": np.load(
            prefix.with_name(prefix.name + "_source_noise.npy")
        ).astype(np.float32),
    }
    session = ort.InferenceSession(args.model, providers=["CPUExecutionProvider"])
    audio = session.run(["audio"], feeds)[0][0, 0].astype(np.float32)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output, audio, 48000, subtype="FLOAT")
    print(f"wrote {output.resolve()} ({audio.size} samples)")


if __name__ == "__main__":
    main()
