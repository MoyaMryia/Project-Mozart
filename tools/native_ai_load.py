#!/usr/bin/env python3
"""运行独立的真实 AI 压力负载，不控制 RVC、USB 或服务。

每个模型使用 CPU 和两个推理线程。每个工作线程先加载模型，再完成一次校验推理。
全部模型通过后，原子发布 ready 文件，再开始计时和连续推理。
退出时删除 ready 文件。
SIGTERM 和 SIGINT 停止提交新推理；当前推理结束后保存报告。
报告分别记录负载时间和收尾时间。处理数和单次耗时包括收尾时完成的推理。
预热不计入处理数。ready 发布和启动期间暂缓处理终止信号。

PocketTTS 调用依据官方 python-api-examples/pocket-tts.py。
Zipformer 使用流式接口；SenseVoice 使用离线接口。
ASR 检查非空文本及时间戳有限性；TTS 检查非空波形及全部样本有限性。
ASR 的数值校验范围为接口时间戳。
"""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import platform
import signal
import statistics
import sys
import tempfile
import threading
import time
import traceback
from typing import Callable


NUM_THREADS = 2
SAMPLE_RATE = 16000
TERMINATION_SIGNALS = frozenset({signal.SIGTERM, signal.SIGINT})
ZIPFORMER_FILES = {
    "encoder": "encoder-epoch-99-avg-1.onnx",
    "decoder": "decoder-epoch-99-avg-1.onnx",
    "joiner": "joiner-epoch-99-avg-1.onnx",
    "tokens": "tokens.txt",
}
SENSEVOICE_FILES = {"model": "model.int8.onnx", "tokens": "tokens.txt"}
POCKET_FILES = {
    "lm_flow": "lm_flow.int8.onnx",
    "lm_main": "lm_main.int8.onnx",
    "encoder": "encoder.onnx",
    "decoder": "decoder.int8.onnx",
    "text_conditioner": "text_conditioner.onnx",
    "vocab_json": "vocab.json",
    "token_scores_json": "token_scores.json",
}


@dataclass
class ModelRun:
    name: str
    kind: str
    model_dir: Path
    assets: list[dict] = field(default_factory=list)
    versions: dict = field(default_factory=dict)
    latencies_s: list[float] = field(default_factory=list)
    last_output: dict = field(default_factory=dict)
    warmup_output: dict = field(default_factory=dict)
    load_s: float | None = None
    warmup_s: float | None = None
    initialized: threading.Event = field(default_factory=threading.Event)

    def report(self) -> dict:
        values = self.latencies_s
        return {
            "name": self.name,
            "kind": self.kind,
            "model_dir": str(self.model_dir),
            "provider": "cpu",
            "num_threads": NUM_THREADS,
            "assets": self.assets,
            "versions": self.versions,
            "load_s": self.load_s,
            "warmup_s": self.warmup_s,
            "warmup_output": self.warmup_output,
            "count": len(values),
            "count_scope": "after_ready_including_drain",
            "latencies_s": values,
            "latency_s": {
                "min": min(values), "median": statistics.median(values),
                "mean": statistics.fmean(values), "max": max(values),
            } if values else None,
            "last_output": self.last_output,
        }


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def asset_paths(run: ModelRun, names: dict[str, str]) -> dict[str, str]:
    paths = {key: str(run.model_dir / name) for key, name in names.items()}
    record_assets(run, [Path(path) for path in paths.values()])
    return paths


def record_assets(run: ModelRun, paths: list[Path]) -> None:
    for path in paths:
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"资产不存在或为空：{path}")
        run.assets.append({"path": str(path), "bytes": path.stat().st_size})


def load_audio(path: Path, np, sf, required_rate: int | None = None):
    audio, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    if audio.shape[0] == 0 or not np.isfinite(audio).all():
        raise ValueError(f"音频为空或含非有限值：{path}")
    if required_rate is not None and sample_rate != required_rate:
        raise ValueError(f"ASR 输入要求 {required_rate} Hz，实际为 {sample_rate} Hz：{path}")
    return np.ascontiguousarray(audio[:, 0]), sample_rate


def load_model(run: ModelRun, wav: Path, text: str, reference: Path | None) -> Callable[[], dict]:
    import numpy as np
    import sherpa_onnx as sherpa
    import soundfile as sf

    run.versions = {
        "numpy": np.__version__, "sherpa_onnx": sherpa.__version__,
        "soundfile": sf.__version__, "libsndfile": sf.__libsndfile_version__,
    }
    if run.kind == "pocket-tts":
        paths = asset_paths(run, POCKET_FILES)
        record_assets(run, [reference])
        samples, sample_rate = load_audio(reference, np, sf)
        config = sherpa.OfflineTtsConfig(model=sherpa.OfflineTtsModelConfig(
            pocket=sherpa.OfflineTtsPocketModelConfig(**paths),
            num_threads=NUM_THREADS, provider="cpu",
        ))
        tts = sherpa.OfflineTts(config)
        generation = sherpa.GenerationConfig()
        generation.reference_audio = samples
        generation.reference_sample_rate = sample_rate
        generation.num_steps = 5

        def infer_tts() -> dict:
            audio = tts.generate(text, generation)
            output = np.asarray(audio.samples)
            if output.size == 0 or not np.isfinite(output).all() or audio.sample_rate <= 0:
                raise ValueError("PocketTTS 输出为空、含非有限值或采样率无效")
            return {
                "samples": int(output.size), "sample_rate": audio.sample_rate,
                "duration_s": output.size / audio.sample_rate,
                "peak": float(np.abs(output).max()),
                "rms": float(np.sqrt(np.mean(output.astype(np.float64) ** 2))),
                "samples_finite": True,
            }

        return infer_tts

    paths = asset_paths(run, ZIPFORMER_FILES if run.kind == "zipformer" else SENSEVOICE_FILES)
    record_assets(run, [wav])
    samples, sample_rate = load_audio(wav, np, sf, SAMPLE_RATE)
    if run.kind == "zipformer":
        recognizer = sherpa.OnlineRecognizer.from_transducer(
            **paths, num_threads=NUM_THREADS, provider="cpu", sample_rate=SAMPLE_RATE,
            decoding_method="greedy_search",
        )
        # 流式模型需要尾部静音，才能解码最后一个块。
        tail = np.zeros(int(0.3 * sample_rate), dtype=np.float32)

        def decode():
            stream = recognizer.create_stream()
            stream.accept_waveform(sample_rate, samples)
            stream.accept_waveform(sample_rate, tail)
            stream.input_finished()
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)
            return recognizer.get_result_all(stream)
    else:
        recognizer = sherpa.OfflineRecognizer.from_sense_voice(
            **paths, num_threads=NUM_THREADS, provider="cpu", sample_rate=SAMPLE_RATE,
            language="auto", use_itn=True,
        )

        def decode():
            stream = recognizer.create_stream()
            stream.accept_waveform(sample_rate, samples)
            recognizer.decode_stream(stream)
            return stream.result

    def infer_asr() -> dict:
        result = decode()
        timestamps = np.asarray(result.timestamps, dtype=np.float64)
        if not result.text.strip() or not np.isfinite(timestamps).all():
            raise ValueError(f"{run.kind} 输出文本为空或时间戳含非有限值")
        return {
            "text": result.text.strip(), "timestamps": timestamps.tolist(),
            "timestamps_finite": True, "input_duration_s": len(samples) / sample_rate,
        }

    return infer_asr


def model_worker(run: ModelRun, loader: Callable[[], Callable[[], dict]],
                 start: threading.Event, stop: threading.Event) -> None:
    # 模型及其原生子线程继承屏蔽集，由主线程统一处理终止信号。
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, TERMINATION_SIGNALS)
    try:
        began = time.monotonic()
        infer = loader()
        run.load_s = time.monotonic() - began
        began = time.monotonic()
        run.warmup_output = infer()
        run.warmup_s = time.monotonic() - began
        run.initialized.set()
        start.wait()
        while not stop.is_set():
            began = time.monotonic()
            output = infer()
            run.latencies_s.append(time.monotonic() - began)
            run.last_output = output
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def failure_report(futures: dict[str, Future]) -> list[dict]:
    errors = []
    for name, future in futures.items():
        error = future.exception()
        if error is not None:
            errors.append({
                "worker": name, "type": type(error).__name__, "reason": str(error),
                "traceback": "".join(traceback.format_exception(type(error), error, error.__traceback__)),
            })
    return errors


def run_load(runs: list[ModelRun], loaders: list[Callable], seconds: float,
             ready_file: Path, stop: threading.Event, report: dict) -> None:
    start = threading.Event()
    futures = {}
    began = None
    stopped = None

    def worker_done(future: Future) -> None:
        if future.exception() is not None:
            stop.set()
            start.set()

    with ThreadPoolExecutor(max_workers=len(runs), thread_name_prefix="native-ai") as executor:
        try:
            for run, loader in zip(runs, loaders):
                future = executor.submit(model_worker, run, loader, start, stop)
                future.add_done_callback(worker_done)
                futures[run.name] = future
            while not stop.is_set() and not all(run.initialized.is_set() for run in runs):
                stop.wait(0.05)
            if not stop.is_set():
                ready = {
                    "pid": os.getpid(), "state": "ready", "seconds": seconds,
                    "models": [run.report() for run in runs],
                }
                previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, TERMINATION_SIGNALS)
                try:
                    if not stop.is_set():
                        atomic_json(ready_file, ready)
                        began = time.monotonic()
                        report["started_monotonic_s"] = began
                        report["ready"] = True
                        start.set()
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                if began is not None:
                    stop.wait(max(0.0, seconds - (time.monotonic() - began)))
        except OSError as error:
            report["errors"].append({
                "worker": "coordinator", "type": type(error).__name__, "reason": str(error),
            })
        finally:
            stopped = time.monotonic()
            stop.set()
            start.set()
    report["elapsed_s"] = stopped - began if began is not None else 0.0
    report["elapsed_with_drain_s"] = time.monotonic() - began if began is not None else 0.0
    report["errors"].extend(failure_report(futures))
    for run in runs:
        if not run.latencies_s:
            report["errors"].append({"worker": run.name, "reason": "计时阶段没有完成有效推理"})


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asr-model", type=Path, required=True, help="首个 ASR 模型目录")
    parser.add_argument("--asr-kind", choices=["zipformer", "sense-voice"], default="zipformer")
    second = parser.add_mutually_exclusive_group(required=True)
    second.add_argument("--tts-model", type=Path, help="PocketTTS int8 模型目录")
    second.add_argument("--second-asr-model", type=Path, help="第二个真实 ASR 模型目录")
    parser.add_argument("--second-asr-kind", choices=["zipformer", "sense-voice"], default="sense-voice")
    parser.add_argument("--tts-reference", type=Path, help="默认使用 PocketTTS test_wavs/bria.wav")
    parser.add_argument("--tts-text", default="The audio system is processing speech in real time.")
    parser.add_argument("--wav", type=Path, required=True, help="16 kHz 语音 WAV；立体声取首通道")
    parser.add_argument("--seconds", type=float, default=60.0, help="ready 发布后的负载秒数")
    parser.add_argument("--output", type=Path, required=True, help="JSON 结果路径")
    parser.add_argument("--ready-file", type=Path, required=True, help="原子发布的就绪文件")
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds 必须为有限正数")
    if not args.tts_text.strip():
        parser.error("--tts-text 必须为非空文本")

    def resolve_path(path: Path, option: str) -> Path:
        try:
            return path.expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as error:
            parser.error(f"{option} 路径解析失败：{path}：{error}")

    for key in ["asr_model", "tts_model", "second_asr_model", "tts_reference", "wav", "output", "ready_file"]:
        value = getattr(args, key)
        if value is not None:
            setattr(args, key, resolve_path(value, "--" + key.replace("_", "-")))
    if args.output == args.ready_file:
        parser.error("--output 和 --ready-file 必须使用不同路径")
    if args.second_asr_model == args.asr_model and args.second_asr_kind == args.asr_kind:
        parser.error("第二个 ASR 必须使用不同模型")

    models = [(args.asr_kind, args.asr_model)]
    inputs = {args.wav}
    if args.tts_model:
        models.append(("pocket-tts", args.tts_model))
        if args.tts_reference is None:
            args.tts_reference = resolve_path(args.tts_model / "test_wavs/bria.wav", "--tts-reference")
        inputs.add(args.tts_reference)
    else:
        models.append((args.second_asr_kind, args.second_asr_model))
    files = {"zipformer": ZIPFORMER_FILES, "sense-voice": SENSEVOICE_FILES, "pocket-tts": POCKET_FILES}
    for kind, model_dir in models:
        inputs.update(resolve_path(model_dir / name, f"{kind} 资产") for name in files[kind].values())
    for option, destination in [("--output", args.output), ("--ready-file", args.ready_file)]:
        if destination in inputs:
            parser.error(f"{option} 与输入音频或模型资产路径重合：{destination}")
        if any(destination.is_relative_to(model_dir) for _, model_dir in models):
            parser.error(f"{option} 必须位于所选模型目录树之外：{destination}")
    return args


def main(argv=None) -> int:
    args = arguments(argv)
    stop = threading.Event()
    report = {
        "passed": False, "ready": False, "errors": [], "signal": None,
        "requested_seconds": args.seconds, "elapsed_s": 0.0, "elapsed_with_drain_s": 0.0,
        "python": sys.version, "platform": platform.platform(),
        "pid": os.getpid(), "wav": str(args.wav),
        "tts_text": args.tts_text if args.tts_model else None,
    }
    runs = [ModelRun("asr", args.asr_kind, args.asr_model)]
    reference = None
    if args.tts_model:
        runs.append(ModelRun("tts", "pocket-tts", args.tts_model))
        reference = args.tts_reference
    else:
        runs.append(ModelRun("second_asr", args.second_asr_kind, args.second_asr_model))

    def on_signal(number, _frame):
        report["signal"] = signal.Signals(number).name
        stop.set()

    previous = {number: signal.signal(number, on_signal) for number in TERMINATION_SIGNALS}
    try:
        try:
            args.ready_file.unlink(missing_ok=True)
            loaders = [lambda run=run: load_model(run, args.wav, args.tts_text, reference) for run in runs]
            run_load(runs, loaders, args.seconds, args.ready_file, stop, report)
        except OSError as error:
            report["errors"].append({
                "worker": "coordinator", "type": type(error).__name__, "reason": str(error),
            })
        finally:
            try:
                args.ready_file.unlink(missing_ok=True)
            except OSError as error:
                report["errors"].append({
                    "worker": "ready_cleanup", "type": type(error).__name__, "reason": str(error),
                })
        report["models"] = [run.report() for run in runs]
        report["passed"] = report["ready"] and not report["errors"]
        atomic_json(args.output, report)
        print(json.dumps({"passed": report["passed"], "output": str(args.output), "errors": report["errors"]},
                         ensure_ascii=False), flush=True)
        return 0 if report["passed"] else 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    sys.exit(main())
