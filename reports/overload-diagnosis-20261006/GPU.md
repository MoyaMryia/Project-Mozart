> 历史记录：本文保留当时的测量、日志与审查原文。当前部署和接口以[维护文档](../../README.md)为准。

# CPU load and GPU offload feasibility — 2026-10-06

## Measured CPU load

There was no sustained aggregate CPU saturation in the two measured translated-speech probes. Median utilization was 53–54% across six cores, with peaks of 78–81%. TTS used approximately 2.4 core equivalents and was the largest measured CPU consumer. Aggregate spare capacity does not remove single-model, sequential-generation or playback limits. The original 30-minute run did not record CPU utilization, so this is not a claim about every moment of that run.

## Current routing

| Component | Current routing | Offload assessment |
| --- | --- | --- |
| Qwen translation | CUDA, launcher requests all layers on GPU | Already offloaded |
| PocketTTS reference voice | sherpa-onnx CPU provider | Largest CPU target; GPU performance of these int8 assets remains unverified |
| Zipformer and optional SenseVoice | CPU inference | Secondary candidates after TTS; model/operator support and full-stack contention must be measured |
| Audio capture, preprocessing, queues and playback | CPU | Keep lightweight scheduling and I/O on CPU |
| RVC in translated-speech probes | Disabled | Separate native GPU path; adding it changes the resource envelope |

## Installed-runtime check

Read-only inspection of `jetson-wg` found sherpa-onnx 1.13.6 in Mozart's Python 3.12 environment. Its extension links its own bundled `libonnxruntime.so`. The separate Python ONNX Runtime 1.27.0 package exposes only Azure and CPU execution providers; replacing that separate package alone does not establish GPU support in sherpa.

A bounded isolated initialization of the actual cached PocketTTS model with `provider='cuda'` succeeded only by falling back to CPU. The runtime explicitly reported:

```text
Available providers: CPUExecutionProvider, . Fallback to cpu!
```

No synthesis or playback was requested, no package was installed, and no production provider setting changed. The initialization process exited normally.

The Jetson reports L4T R39.2.0 and `/usr/local/cuda-13.2`. Any new runtime must match this arm64 deployment. [Sherpa's GPU build documentation](https://k2-fsa.github.io/sherpa/onnx/install/linux.html) describes GPU-enabled builds, including an older CUDA 12.6 Jetson example; that example is not verification of this CUDA 13.2 installation. The [deployed-version provider selection source](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.6/sherpa-onnx/csrc/session.cc) confirms fallback when CUDA is unavailable, and [PocketTTS model source](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.6/sherpa-onnx/csrc/offline-tts-pocket-model.cc) passes the configured session options to its model sessions.

## Recommended next step

Build or obtain a compatible GPU-enabled sherpa/ONNX Runtime in an isolated environment. Benchmark the identical saved TTS jobs first, confirming actual operator placement rather than successful model construction. Check wall time, CPU load, shared RAM, numerical/audio quality, and competition with the CUDA translator. The current int8 models are not proof of GPU acceleration; their quantized operator coverage needs checking, and an alternate model precision may be necessary. [ONNX Runtime's quantization guidance](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html) distinguishes CPU and GPU quantization paths.

GPU offload could reduce synthesis CPU load and delay. It cannot remove excess playback duration or the current shared admission/late-discard behavior. No GPU speedup is yet measured.
