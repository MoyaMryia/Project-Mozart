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
    set_wav_peak_timestamp,
)


IN_RATE = 16_000
OUT_RATE = 48_000
IN_SAMPLES = 320
OUT_SAMPLES = 960
WINDOW = 32_000
CROSSFADE_OUT = 2_880
HOP = WINDOW - CROSSFADE_OUT // 3
EMIT = HOP * 3
# An 80-sample suffix yields the two HuBERT frames otherwise lost when an
# inference span is an exact multiple of 320 samples. The suffix is discarded.
HUBERT_GUARD = 80


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


class StreamingPipeline(Pipeline):
    """Preserve all-unvoiced RMVPE output without using an exception path."""

    def get_f0(self, x, p_len, f0_up_key, f0_method):
        if f0_method != "rmvpe":
            return super().get_f0(x, p_len, f0_up_key, f0_method)

        f0 = self.model_rmvpe.infer_from_audio(x, thred=0.03)
        voiced = f0 != 0
        if np.any(voiced):
            f0[~voiced] = np.interp(
                np.where(~voiced)[0], np.where(voiced)[0], f0[voiced]
            )
        f0 *= pow(2, f0_up_key / 12)
        continuous = f0.copy()
        f0_mel = 1127 * np.log(1 + f0 / 700)
        f0_mel_min = 1127 * np.log(1 + 50 / 700)
        f0_mel_max = 1127 * np.log(1 + 1100 / 700)
        positive = f0_mel > 0
        f0_mel[positive] = (
            (f0_mel[positive] - f0_mel_min) * 254
            / (f0_mel_max - f0_mel_min)
            + 1
        )
        f0_mel[f0_mel <= 1] = 1
        f0_mel[f0_mel > 255] = 255
        return np.rint(f0_mel).astype(np.int32), continuous


class StreamingReference:
    """Generate either the legacy backend contract or a context-rich stream."""

    def __init__(self, pipeline, hubert, generator, tensor_dir, skip_silence,
                 rmvpe_capture_state, target_samples=WINDOW,
                 left_context_samples=0, right_context_samples=0,
                 crossfade_output_samples=CROSSFADE_OUT, full_history=False,
                 generator_rng_state=None):
        self.pipeline = pipeline
        self.hubert = hubert
        self.generator = generator
        self.tensor_dir = tensor_dir
        self.skip_silence = skip_silence
        self.rmvpe_capture_state = rmvpe_capture_state
        self.target_samples = target_samples
        self.left_context_samples = left_context_samples
        self.right_context_samples = right_context_samples
        self.crossfade_output_samples = crossfade_output_samples
        self.full_history = full_history
        self.generator_rng_state = generator_rng_state
        self.hop_samples = target_samples - crossfade_output_samples // 3
        self.emit_samples = self.hop_samples * 3
        self.context_mode = (
            target_samples != WINDOW
            or left_context_samples != 0
            or right_context_samples != 0
            or crossfade_output_samples != CROSSFADE_OUT
            or full_history
        )
        self.samples = np.zeros(0, dtype=np.float32)
        self.pushed = 0
        self.owned = 0
        self.have_window = False
        self.window_voiced = False
        self.output = np.zeros(0, dtype=np.float32)
        self.emitted = []
        self.tail = np.zeros(crossfade_output_samples, dtype=np.float32)
        self.tail_valid = False
        self.blocks = 0
        self.skipped_blocks = 0
        self.windows = 0
        self.next_window_start = 0

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
        if self.context_mode:
            self._try_process_with_context()
            return

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
        self.emitted.append(emit.copy())
        self.output = np.concatenate((self.output, emit))

    def _try_process_with_context(self) -> None:
        target_start = self.next_window_start
        target_end = target_start + self.target_samples
        inference_start = (
            0
            if self.full_history
            else max(0, target_start - self.left_context_samples)
        )
        inference_end = target_end + self.right_context_samples + HUBERT_GUARD
        if self.pushed < inference_end:
            return

        window = np.ascontiguousarray(
            self.samples[inference_start:inference_end], dtype=np.float32
        )
        target_offset = target_start - inference_start
        target = window[target_offset:target_offset + self.target_samples]
        voiced = bool(np.any(target))
        skipped = not voiced or (self.skip_silence and not self.window_voiced)
        self.window_voiced = False

        block_dir = self.tensor_dir / f"block_{self.windows:03d}"
        block_dir.mkdir(parents=True, exist_ok=True)
        np.save(block_dir / "input_window_16k.npy", window)
        (block_dir / "metadata.json").write_text(json.dumps({
            "window_index": self.windows,
            "input_samples_seen": self.pushed,
            "target_start_sample": target_start,
            "target_samples": self.target_samples,
            "inference_start_sample": inference_start,
            "inference_samples": int(window.size),
            "left_context_samples": target_offset,
            "right_context_samples": self.right_context_samples,
            "full_history": self.full_history,
            "hubert_guard_samples": HUBERT_GUARD,
            "input_frames_seen": self.pushed // IN_SAMPLES,
            "voiced": voiced,
            "skipped": skipped,
        }, indent=2) + "\n")
        self.generator.tensor_dir = block_dir
        self.rmvpe_capture_state["dir"] = block_dir
        self.windows += 1
        self.next_window_start += self.hop_samples

        if skipped:
            converted = np.zeros(self.target_samples * 3, dtype=np.float32)
            self.skipped_blocks += 1
        else:
            if window.size > self.pipeline.t_max:
                raise ValueError(
                    "streaming prefix exceeds the original RVC unsplit limit: "
                    f"{window.size} > {self.pipeline.t_max} samples"
                )
            if self.generator_rng_state is not None:
                torch.set_rng_state(self.generator_rng_state)
            with torch.inference_mode():
                full_output = self.pipeline.pipeline(
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
            np.save(block_dir / "pipeline_output_raw_float32.npy", full_output)
            expected_samples = (window.size - HUBERT_GUARD) * 3
            if full_output.size != expected_samples:
                raise ValueError(
                    "context streaming output length mismatch: "
                    f"got {full_output.size}, expected {expected_samples}"
                )
            output_start = target_offset * 3
            converted = full_output[
                output_start:output_start + self.target_samples * 3
            ].copy()
            if converted.size != self.target_samples * 3:
                raise ValueError(
                    "context streaming target crop is incomplete: "
                    f"got {converted.size}, expected {self.target_samples * 3}"
                )
            self.blocks += 1

        if self.tail_valid:
            emit = converted[:self.emit_samples].copy()
            weights = (
                np.arange(self.crossfade_output_samples, dtype=np.float32)
                / self.crossfade_output_samples
            )
            emit[:self.crossfade_output_samples] = (
                self.tail * (1.0 - weights)
                + converted[:self.crossfade_output_samples] * weights
            )
        else:
            emit = converted[:self.emit_samples].copy()
        self.tail = converted[self.emit_samples:].copy()
        self.tail_valid = True
        self.emitted.append(emit.copy())
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
    parser.add_argument(
        "--audition-output",
        help="optional latency-free content timeline assembled from emitted blocks",
    )
    parser.add_argument("--tensor-dir", default=str(ROOT / "tensors" / "qiqi_zh_streaming"))
    parser.add_argument("--flush-seconds", type=float, default=2.0)
    parser.add_argument("--skip-silence", action="store_true",
                        help="match a backend with input.meta.vad_enabled=true")
    parser.add_argument("--pace", action="store_true", help="wait 20 ms between conceptual input frames")
    parser.add_argument("--deterministic-generator", action="store_true",
                        help="use the export-alignment mean path so the reference "
                        "matches the deterministic ONNX/TRT generator")
    parser.add_argument("--target-seconds", type=float, default=2.0)
    parser.add_argument("--left-context-seconds", type=float, default=0.0)
    parser.add_argument("--right-context-seconds", type=float, default=0.0)
    parser.add_argument("--crossfade-seconds", type=float, default=0.06)
    parser.add_argument(
        "--full-history",
        action="store_true",
        help="recompute from the stream origin for maximum prefix consistency",
    )
    parser.add_argument(
        "--reset-generator-rng",
        action="store_true",
        help="restore the post-load Torch RNG state before each window",
    )
    parser.add_argument("--seed", type=int, default=114514)
    parser.add_argument("--wav-peak-timestamp", type=int, default=None)
    args = parser.parse_args()

    target_samples = round(args.target_seconds * IN_RATE)
    left_context_samples = round(args.left_context_seconds * IN_RATE)
    right_context_samples = round(args.right_context_seconds * IN_RATE)
    crossfade_output_samples = round(args.crossfade_seconds * OUT_RATE)
    input_lengths = (target_samples, left_context_samples, right_context_samples)
    if target_samples <= 0 or any(
        value < 0 or value % IN_SAMPLES for value in input_lengths
    ):
        raise ValueError(
            "target and context lengths must be non-negative 20 ms multiples"
        )
    if (
        crossfade_output_samples <= 0
        or crossfade_output_samples % OUT_SAMPLES
        or crossfade_output_samples // 3 >= target_samples
    ):
        raise ValueError(
            "crossfade must be a positive 20 ms multiple shorter than the target"
        )
    if args.reset_generator_rng and not args.full_history:
        raise ValueError("reset-generator-rng requires full-history prefix recomputation")
    if args.full_history and left_context_samples:
        raise ValueError("left-context-seconds is redundant with full-history")

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    tensor_dir = Path(args.tensor_dir)
    tensor_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    audio = load_input(args.input)
    maximum_unsplit_samples = ReferenceConfig.x_max * IN_RATE
    full_history_limit = (
        audio.size + target_samples + right_context_samples + HUBERT_GUARD
    )
    if args.full_history and full_history_limit > maximum_unsplit_samples:
        raise ValueError(
            "full-history target plus lookahead can exceed the original RVC "
            f"{ReferenceConfig.x_max}-second unsplit limit"
        )
    if args.audition_output and audio.size > maximum_unsplit_samples:
        raise ValueError(
            "audition output currently requires input no longer than "
            f"{ReferenceConfig.x_max} seconds"
        )
    np.save(tensor_dir / "input_16k.npy", audio)
    generator, target_rate = load_generator(
        args.model, deterministic=args.deterministic_generator
    )
    if target_rate != OUT_RATE:
        raise ValueError(f"qiqi streaming reference expects 48 kHz model, got {target_rate}")
    capturing_generator = CapturingGenerator(
        generator, tensor_dir, deterministic=args.deterministic_generator
    )

    hubert_path = ROOT / "assets" / "hubert_base"
    hubert = HubertModelWithFinalProj.from_pretrained(
        str(hubert_path), local_files_only=True, torch_dtype=torch.float32
    ).eval().float()
    rmvpe = RMVPE(str(ROOT / "assets" / "rmvpe" / "rmvpe.pt"), is_half=False, device="cpu")
    pipeline = StreamingPipeline(target_rate, ReferenceConfig())
    pipeline.model_rmvpe = rmvpe
    capture_state = {"dir": tensor_dir}
    install_block_rmvpe_capture(rmvpe, tensor_dir, capture_state)
    generator_rng_state = (
        torch.get_rng_state().clone() if args.reset_generator_rng else None
    )

    stream = StreamingReference(
        pipeline, hubert, capturing_generator, tensor_dir, args.skip_silence,
        capture_state, target_samples, left_context_samples,
        right_context_samples, crossfade_output_samples, args.full_history,
        generator_rng_state
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
    if args.wav_peak_timestamp is not None:
        set_wav_peak_timestamp(output_path, args.wav_peak_timestamp)

    audition_path = None
    audition_samples = 0
    if args.audition_output:
        audition_path = Path(args.audition_output)
        audition_path.parent.mkdir(parents=True, exist_ok=True)
        audition = (
            np.concatenate(stream.emitted)
            if stream.emitted
            else np.zeros(0, dtype=np.float32)
        )
        hubert_frames = (audio.size + 2 * IN_RATE - 400) // 320 + 1
        audition_samples = (hubert_frames * 2 - 200) * (OUT_RATE // 100)
        if audition.size < audition_samples:
            raise ValueError(
                "stream did not emit enough audition audio: "
                f"got {audition.size}, expected {audition_samples}"
            )
        audition = audition[:audition_samples]
        sf.write(audition_path, audition, OUT_RATE, subtype="FLOAT")
        if args.wav_peak_timestamp is not None:
            set_wav_peak_timestamp(audition_path, args.wav_peak_timestamp)

    metadata = {
        "input": str(Path(args.input).resolve()),
        "model": str(Path(args.model).resolve()),
        "output": str(output_path.resolve()),
        "input_samples_16k": int(audio.size),
        "input_frames": frame_count,
        "flush_seconds": args.flush_seconds,
        "target_rate": target_rate,
        "output_samples": int(result.size),
        "audition_output": str(audition_path.resolve()) if audition_path else None,
        "audition_samples": audition_samples,
        "stream_blocks": stream.blocks,
        "skipped_blocks": stream.skipped_blocks,
        "stream_windows": stream.windows,
        "elapsed_seconds": time.monotonic() - started,
        "window_samples": stream.target_samples,
        "hop_samples": stream.hop_samples,
        "crossfade_output_samples": stream.crossfade_output_samples,
        "left_context_samples": left_context_samples,
        "right_context_samples": right_context_samples,
        "full_history": args.full_history,
        "reset_generator_rng": args.reset_generator_rng,
        "hubert_guard_samples": HUBERT_GUARD if stream.context_mode else 0,
        "seed": args.seed,
        "wav_peak_timestamp": args.wav_peak_timestamp,
        "output_is_raw_stream": True,
    }
    metadata_path = output_path.with_suffix(".json")
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
