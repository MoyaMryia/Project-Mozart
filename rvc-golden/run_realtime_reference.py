#!/usr/bin/env python3
"""Run the upstream RVC realtime path without its audio-device GUI."""

import argparse
import json
import os
import sys
import time
import types
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torch.nn.functional as F


ROOT = Path(__file__).resolve().parent
RVC_SOURCE = Path(
    os.environ.get("RVC_SOURCE", "/home/moyamryia/mozart-archive/RVC")
)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(RVC_SOURCE))

from run_streaming_reference import IN_RATE, OUT_RATE, load_input  # noqa: E402


MODEL_FRAME = IN_RATE // 100
OUTPUT_FRAME = OUT_RATE // 100


class RuntimeConfig:
    def __init__(self, device: str):
        self.device = torch.device(device)
        self.is_half = self.device.type == "cuda"


def install_optional_torchaudio_stub() -> None:
    """Let upstream import its unused formant-resampler on this test host."""
    try:
        import torchaudio  # noqa: F401
        return
    except RuntimeError as error:
        if "compiled with different CUDA versions" not in str(error):
            raise

    for name in tuple(sys.modules):
        if name == "torchaudio" or name.startswith("torchaudio."):
            del sys.modules[name]

    class UnavailableResample:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                "formant resampling requires a TorchAudio build matching PyTorch"
            )

    torchaudio = types.ModuleType("torchaudio")
    transforms = types.ModuleType("torchaudio.transforms")
    transforms.Resample = UnavailableResample
    torchaudio.transforms = transforms
    sys.modules["torchaudio"] = torchaudio
    sys.modules["torchaudio.transforms"] = transforms


def quantize_samples(seconds: float, sample_rate: int, quantum: int) -> int:
    samples = round(seconds * sample_rate / quantum) * quantum
    if samples <= 0:
        raise ValueError("realtime durations must be positive model-frame multiples")
    return samples


def sola_merge(
    inferred: torch.Tensor,
    previous: torch.Tensor,
    fade_in: torch.Tensor,
    fade_out: torch.Tensor,
    denominator_kernel: torch.Tensor,
    block_samples: int,
    search_samples: int,
) -> tuple[torch.Tensor, torch.Tensor, int]:
    crossfade_samples = previous.numel()
    search = inferred[None, None, : crossfade_samples + search_samples].float()
    numerator = F.conv1d(search, previous[None, None, :])
    denominator = torch.sqrt(F.conv1d(search.square(), denominator_kernel) + 1e-8)
    offset = int(torch.argmax(numerator[0, 0] / denominator[0, 0]).item())

    aligned = inferred[offset:].clone()
    required = block_samples + crossfade_samples
    if aligned.numel() < required:
        aligned = F.pad(aligned, (0, required - aligned.numel()))
    aligned[:crossfade_samples] = (
        aligned[:crossfade_samples] * fade_in + previous * fade_out
    )
    return (
        aligned[:block_samples],
        aligned[block_samples:block_samples + crossfade_samples].clone(),
        offset,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", default=str(ROOT / "input" / "qiqi-espeak-pinyin-zh-en-mixed.wav")
    )
    parser.add_argument(
        "--model",
        default=(
            "/home/moyamryia/mozart-archive/RVC_Model_Collection/"
            "48k（新版）/原神/中文/七七/七七.pth"
        ),
    )
    parser.add_argument(
        "--output", default=str(ROOT / "output" / "qiqi-upstream-realtime.wav")
    )
    parser.add_argument("--block-seconds", type=float, default=0.25)
    parser.add_argument("--extra-seconds", type=float, default=2.5)
    parser.add_argument("--crossfade-seconds", type=float, default=0.05)
    parser.add_argument("--sola-search-seconds", type=float, default=0.01)
    parser.add_argument("--f0-method", choices=("pm", "rmvpe", "fcpe"), default="rmvpe")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--seed", type=int, default=114514)
    parser.add_argument("--max-seconds", type=float)
    args = parser.parse_args()

    if args.device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is unavailable")
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    block_in = quantize_samples(args.block_seconds, IN_RATE, MODEL_FRAME)
    extra_in = quantize_samples(args.extra_seconds, IN_RATE, MODEL_FRAME)
    crossfade_out = quantize_samples(
        args.crossfade_seconds, OUT_RATE, OUTPUT_FRAME
    )
    search_out = quantize_samples(
        args.sola_search_seconds, OUT_RATE, OUTPUT_FRAME
    )
    crossfade_in = crossfade_out * IN_RATE // OUT_RATE
    search_in = search_out * IN_RATE // OUT_RATE
    inference_samples = block_in + extra_in + crossfade_in + search_in
    if inference_samples % MODEL_FRAME:
        inference_samples += MODEL_FRAME - inference_samples % MODEL_FRAME

    audio = load_input(args.input)
    if args.max_seconds is not None:
        audio = audio[: round(args.max_seconds * IN_RATE)]

    import infer.hubert as hubert_module  # noqa: E402
    from infer.rmvpe import RMVPE  # noqa: E402
    install_optional_torchaudio_stub()
    original_cwd = Path.cwd()
    try:
        os.chdir(RVC_SOURCE)
        from infer.rtrvc import RVC  # noqa: E402
    finally:
        os.chdir(original_cwd)

    hubert_module.HUBERT_MODEL_PATH = (ROOT / "assets" / "hubert_base").resolve()
    runtime = RuntimeConfig(args.device)
    converter = RVC(0, 0.0, args.model, "", 0.0, runtime)
    if args.f0_method == "rmvpe":
        converter.model_rmvpe = RMVPE(
            str(ROOT / "assets" / "rmvpe" / "rmvpe.pt"),
            is_half=runtime.is_half,
            device=runtime.device,
        )
    if converter.tgt_sr != OUT_RATE:
        raise ValueError(
            f"realtime reference expects a 48 kHz model, got {converter.tgt_sr}"
        )

    input_buffer = torch.zeros(
        inference_samples, device=runtime.device, dtype=torch.float32
    )
    block_out = block_in * OUT_RATE // IN_RATE
    skip_head = extra_in // MODEL_FRAME
    return_length = (block_in + crossfade_in + search_in) // MODEL_FRAME
    fade_in = torch.sin(
        0.5
        * torch.pi
        * torch.linspace(
            0.0, 1.0, crossfade_out,
            device=runtime.device, dtype=torch.float32,
        )
    ).square()
    fade_out = 1.0 - fade_in
    denominator_kernel = torch.ones(
        1, 1, crossfade_out, device=runtime.device, dtype=torch.float32
    )
    sola_buffer = torch.zeros(
        crossfade_out, device=runtime.device, dtype=torch.float32
    )

    output_blocks = []
    inference_ms = []
    sola_offsets = []
    started = time.perf_counter()
    for start in range(0, audio.size, block_in):
        block = np.zeros(block_in, dtype=np.float32)
        count = min(block_in, audio.size - start)
        block[:count] = audio[start:start + count]
        input_buffer[:-block_in] = input_buffer[block_in:].clone()
        input_buffer[-block_in:] = torch.from_numpy(block).to(runtime.device)

        before = time.perf_counter()
        with torch.inference_mode():
            inferred = converter.infer(
                input_buffer,
                block_in,
                skip_head,
                return_length,
                args.f0_method,
            )
        if runtime.device.type == "cuda":
            torch.cuda.synchronize(runtime.device)
        inference_ms.append((time.perf_counter() - before) * 1000.0)

        emitted, sola_buffer, offset = sola_merge(
            inferred,
            sola_buffer,
            fade_in,
            fade_out,
            denominator_kernel,
            block_out,
            search_out,
        )
        output_blocks.append(emitted.cpu().numpy())
        sola_offsets.append(offset)

    output = (
        np.concatenate(output_blocks).astype(np.float32, copy=False)
        if output_blocks
        else np.zeros(0, dtype=np.float32)
    )
    output_samples = (audio.size // MODEL_FRAME) * OUTPUT_FRAME
    output = output[:output_samples]
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output_path, output, OUT_RATE, subtype="FLOAT")

    timings = np.asarray(inference_ms, dtype=np.float64)
    metadata = {
        "input": str(Path(args.input).resolve()),
        "model": str(Path(args.model).resolve()),
        "rvc_source": str(RVC_SOURCE.resolve()),
        "rvc_source_commit": "81eed5e8f68b6bed1789f682fe78cdd324495afc",
        "output": str(output_path.resolve()),
        "device": str(runtime.device),
        "f0_method": args.f0_method,
        "block_samples_16k": block_in,
        "block_seconds": block_in / IN_RATE,
        "extra_past_samples_16k": extra_in,
        "extra_past_seconds": extra_in / IN_RATE,
        "crossfade_samples_48k": crossfade_out,
        "sola_search_samples_48k": search_out,
        "inference_window_samples_16k": inference_samples,
        "inference_window_seconds": inference_samples / IN_RATE,
        "skip_head_frames": skip_head,
        "return_frames": return_length,
        "blocks": len(output_blocks),
        "output_samples": int(output.size),
        "elapsed_seconds": time.perf_counter() - started,
        "inference_ms": {
            "median": float(np.median(timings)) if timings.size else None,
            "p95": float(np.percentile(timings, 95)) if timings.size else None,
            "max": float(np.max(timings)) if timings.size else None,
        },
        "sola_offset_samples_48k": {
            "median": float(np.median(sola_offsets)) if sola_offsets else None,
            "max": int(max(sola_offsets)) if sola_offsets else None,
        },
        "seed": args.seed,
    }
    output_path.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
