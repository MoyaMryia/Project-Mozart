#!/usr/bin/env python3
"""Run the original PyTorch RVC as a deterministic realtime-style stream.

This is intentionally separate from the offline reference runner.  Each
20 ms input frame is pushed into a sample ring, a fixed 2 s window is inferred
when ready, and the generated blocks are consumed exactly like Mozart's
AudioWorker.  Every block's F0, generator inputs, and generator output are
saved for later Golden-vs-ONNX-vs-backend comparisons.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import torch


ROOT = Path(__file__).resolve().parent
RVC_SOURCE = Path(__import__("os").environ.get("RVC_SOURCE", "/home/moyamryia/mozart-archive/RVC"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(RVC_SOURCE))

from run_reference import (  # noqa: E402
    CapturingGenerator,
    HubertModelWithFinalProj,
    Pipeline,
    ReferenceConfig,
    RMVPE,
    load_generator,
)


IN_RATE = 16_000
OUT_RATE = 48_000
IN_SAMPLES = 320
OUT_SAMPLES = 960
WINDOW = 32_000
CROSSFADE_OUT = 2_880
HOP = WINDOW - CROSSFADE_OUT // 3
EMIT = HOP * 3


def load_input(path: str) -> np.ndarray:
    audio, source_rate = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if source_rate != IN_RATE:
        audio = librosa.resample(audio, orig_sr=source_rate, target_sr=IN_RATE)
    audio = np.asarray(audio, dtype=np.float32)
    peak = float(np.max(np.abs(audio), initial=0.0)) / 0.95
    if peak > 1.0:
        audio /= peak
    return np.ascontiguousarray(audio, dtype=np.float32)


class StreamingReference:
    """The content-side equivalent of StreamingRvc + AudioWorker."""

    def __init__(self, pipeline, hubert, generator, tensor_dir, skip_silence,
                 rmvpe_capture_state):
        self.pipeline = pipeline
        self.hubert = hubert
        self.generator = generator
        self.tensor_dir = tensor_dir
        self.skip_silence = skip_silence
        self.rmvpe_capture_state = rmvpe_capture_state
        self.samples = np.zeros(0, dtype=np.float32)
        self.pushed = 0
        self.owned = 0
        self.have_window = False
        self.window_voiced = False
        self.output = np.zeros(0, dtype=np.float32)
        self.tail = np.zeros(CROSSFADE_OUT, dtype=np.float32)
        self.tail_valid = False
        self.blocks = 0
        self.skipped_blocks = 0
        self.windows = 0

    def push(self, frame: np.ndarray, voiced: bool) -> np.ndarray:
        self.samples = np.concatenate((self.samples, frame))
        self.pushed += frame.size
        self.window_voiced |= voiced
        self._try_process()
        block = np.zeros(OUT_SAMPLES, dtype=np.float32)
        count = min(OUT_SAMPLES, self.output.size)
        if count:
            block[:count] = self.output[:count]
            self.output = self.output[count:]
        return block

    def _latest_window(self) -> np.ndarray:
        window = np.zeros(WINDOW, dtype=np.float32)
        take = min(WINDOW, self.samples.size)
        if take:
            window[-take:] = self.samples[-take:]
        return window

    def _try_process(self) -> None:
        ready = (self.pushed - self.owned >= HOP) if self.have_window else self.pushed >= WINDOW
        if not ready:
            return
        window = self._latest_window()
        voiced = self.window_voiced
        self.window_voiced = False
        self.owned = self.pushed
        self.have_window = True
        block_dir = self.tensor_dir / f"block_{self.windows:03d}"
        block_dir.mkdir(parents=True, exist_ok=True)
        np.save(block_dir / "input_window_16k.npy", window)
        (block_dir / "metadata.json").write_text(json.dumps({
            "window_index": self.windows,
            "input_samples_seen": self.pushed,
            "window_start_sample": max(0, self.pushed - WINDOW),
            "window_samples": WINDOW,
            "input_frames_seen": self.pushed // IN_SAMPLES,
            "voiced": voiced,
            "skipped": self.skip_silence and not voiced,
        }, indent=2) + "\n")
        self.generator.tensor_dir = block_dir
        self.rmvpe_capture_state["dir"] = block_dir
        self.windows += 1

        if self.skip_silence and not voiced:
            converted = np.zeros(WINDOW * 3, dtype=np.float32)
            self.tail.fill(0.0)
            self.tail_valid = False
            self.skipped_blocks += 1
        else:
            # The original Pipeline returns int16-scaled samples.  Convert it
            # back to float32 at the contract boundary, matching the WAV path.
            with torch.inference_mode():
                converted = self.pipeline.pipeline(
                    self.hubert,
                    self.generator,
                    0,
                    window,
                    [0.0, 0.0, 0.0],
                    0,
                    "rmvpe",
                    "",
                    0.0,
                    1,
                    OUT_RATE,
                    OUT_RATE,
                    1.0,
                    "v2",
                    0.33,
                ).astype(np.float32) / 32768.0
            np.save(block_dir / "pipeline_output_raw_float32.npy", converted)
            if converted.size < WINDOW * 3:
                raw_output_samples = converted.size
                converted = np.pad(converted, (0, WINDOW * 3 - converted.size))
                (block_dir / "contract_padding.json").write_text(json.dumps({
                    "raw_output_samples": int(raw_output_samples),
                    "contract_output_samples": WINDOW * 3,
                    "padded_samples": WINDOW * 3 - int(raw_output_samples),
                }, indent=2) + "\n")
            self.blocks += 1

        if self.tail_valid:
            emit = converted[:EMIT].copy()
            weights = np.arange(CROSSFADE_OUT, dtype=np.float32) / CROSSFADE_OUT
            emit[:CROSSFADE_OUT] = (
                self.tail * (1.0 - weights) + converted[:CROSSFADE_OUT] * weights
            )
        else:
            emit = converted[:EMIT].copy()
        self.tail = converted[WINDOW * 3 - CROSSFADE_OUT:WINDOW * 3].copy()
        self.tail_valid = True
        self.output = np.concatenate((self.output, emit))

    def flush(self, seconds: float) -> list[np.ndarray]:
        frames = max(0, round(seconds * IN_RATE / IN_SAMPLES))
        result = []
        silence = np.zeros(IN_SAMPLES, dtype=np.float32)
        for _ in range(frames):
            result.append(self.push(silence, voiced=False))
        return result


def install_block_rmvpe_capture(rmvpe, tensor_dir, state):
    original_extract = rmvpe.extract_mel
    original_decode = rmvpe.decode

    def capture_mel(samples, center=True):
        mel = original_extract(samples, center=center)
        np.save(state["dir"] / "rmvpe_mel.npy", mel.detach().cpu().numpy())
        return mel

    def capture_f0(hidden, thred=0.03):
        np.save(state["dir"] / "rmvpe_salience.npy", np.asarray(hidden))
        f0 = original_decode(hidden, thred=thred)
        np.save(state["dir"] / "rmvpe_f0.npy", f0)
        return f0

    rmvpe.extract_mel = capture_mel
    rmvpe.decode = capture_f0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(ROOT / "input" / "qiqi-espeak-zh-en-mixed.wav"))
    parser.add_argument("--model", default="/home/moyamryia/mozart-archive/RVC_Model_Collection/48k（新版）/原神/中文/七七/七七.pth")
    parser.add_argument("--output", default=str(ROOT / "output" / "qiqi-zh-espeak-streaming-python-reference.wav"))
    parser.add_argument("--tensor-dir", default=str(ROOT / "tensors" / "qiqi_zh_streaming"))
    parser.add_argument("--flush-seconds", type=float, default=2.0)
    parser.add_argument("--skip-silence", action="store_true",
                        help="match a backend with input.meta.vad_enabled=true")
    parser.add_argument("--pace", action="store_true", help="wait 20 ms between conceptual input frames")
    args = parser.parse_args()

    torch.manual_seed(114514)
    np.random.seed(114514)
    tensor_dir = Path(args.tensor_dir)
    tensor_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    audio = load_input(args.input)
    np.save(tensor_dir / "input_16k.npy", audio)
    generator, target_rate = load_generator(args.model)
    if target_rate != OUT_RATE:
        raise ValueError(f"qiqi streaming reference expects 48 kHz model, got {target_rate}")
    capturing_generator = CapturingGenerator(generator, tensor_dir)

    hubert_path = ROOT / "assets" / "hubert_base"
    hubert = HubertModelWithFinalProj.from_pretrained(
        str(hubert_path), local_files_only=True, torch_dtype=torch.float32
    ).eval().float()
    rmvpe = RMVPE(str(ROOT / "assets" / "rmvpe" / "rmvpe.pt"), is_half=False, device="cpu")
    pipeline = Pipeline(target_rate, ReferenceConfig())
    pipeline.model_rmvpe = rmvpe
    capture_state = {"dir": tensor_dir}
    install_block_rmvpe_capture(rmvpe, tensor_dir, capture_state)

    stream = StreamingReference(
        pipeline, hubert, capturing_generator, tensor_dir, args.skip_silence,
        capture_state
    )
    outputs = []
    started = time.monotonic()
    frame_count = (audio.size + IN_SAMPLES - 1) // IN_SAMPLES
    for frame_idx in range(frame_count):
        start = frame_idx * IN_SAMPLES
        frame = np.zeros(IN_SAMPLES, dtype=np.float32)
        frame[: min(IN_SAMPLES, audio.size - start)] = audio[start:start + IN_SAMPLES]
        outputs.append(stream.push(frame, voiced=True))
        if args.pace:
            time.sleep(0.02)
    outputs.extend(stream.flush(args.flush_seconds))
    result = np.concatenate(outputs) if outputs else np.zeros(0, dtype=np.float32)
    sf.write(output_path, result, OUT_RATE, subtype="FLOAT")

    metadata = {
        "input": str(Path(args.input).resolve()),
        "model": str(Path(args.model).resolve()),
        "output": str(output_path.resolve()),
        "input_samples_16k": int(audio.size),
        "input_frames": frame_count,
        "flush_seconds": args.flush_seconds,
        "target_rate": target_rate,
        "output_samples": int(result.size),
        "stream_blocks": stream.blocks,
        "skipped_blocks": stream.skipped_blocks,
        "stream_windows": stream.windows,
        "elapsed_seconds": time.monotonic() - started,
        "window_samples": WINDOW,
        "hop_samples": HOP,
        "crossfade_output_samples": CROSSFADE_OUT,
        "output_is_raw_stream": True,
    }
    metadata_path = output_path.with_suffix(".json")
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
