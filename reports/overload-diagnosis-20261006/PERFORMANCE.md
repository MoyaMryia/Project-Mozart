# Mozart performance limits — 2026-10-06

For the user's target of translated speech in a reference voice, the strongest demonstrated limits are output duration, whole-sentence processing, and shared queue admission. PocketTTS has a meaningful CPU cost, but measured generation capacity fits the tested input's average rate. The board was not generally saturated in the controlled full-stack probes. Increasing the TTS thread count produced a small isolated improvement; it does not resolve playback demand or scheduling.

## 1. Latency: most time is after translation

These values come from the saved 30-minute run, using the actual current deployment. Percentiles here use linear interpolation at `(n-1)*p`; small differences from older reports reflect their percentile method.

| Stage | Median | 95th percentile | Measurement population |
| --- | ---: | ---: | --- |
| SenseVoice final refinement | 0.284 s | 0.947 s | 407 caption finals |
| Successful translation | 0.356 s | 1.259 s | 401 translations; failed translations excluded |
| Waiting to start synthesis | 0.474 s | 11.179 s | 274 admitted jobs |
| Successful complete-WAV generation | 2.901 s | 10.656 s | 272 jobs, including subsequently discarded audio |
| Waiting from audio ready to playback | 4.686 s | 14.070 s | 244 completed jobs |
| TTS submission to playback start | **10.734 s** | **25.861 s** | 244 completed jobs |

The stages have different populations and overlap; their medians must not be added to construct an end-to-end latency. Rejected jobs are absent from completed-job latency, so these numbers are conditional on completion.

The 0.284-second ASR figure measures only final refinement computation, not recognition or endpoint waiting. The streaming recognizer normally needs 0.6 seconds of trailing silence after recognized speech, has a 1.2-second silence rule, and forces an utterance endpoint at 12 seconds. See [stt_service.py](/home/moyamryia/Projects/Project-Mozart/tools/stt_service.py:45). Source speech must reach a final endpoint before translation begins. The bridge translates those finals serially, so unusually slow translations can also delay subsequent finals; the maximum recorded successful translation was 5.898 seconds. This is a code-level risk, not a measured attribution of every queue delay.

PocketTTS then produces the whole WAV before playback starts. The generation callback arrived at a median of 1.867 seconds in the long run, but no callback audio is currently played. Therefore it cannot be advertised as audible latency. True source-word-end-to-speaker timestamps and physical output latency were not recorded.

## 2. Throughput: speaking time is a separate limit

The controlled 120-second WAV-only replay completed all 22 valid translated jobs:

- Generated speech: **132.56 seconds**.
- Recorded synthesis computation: **97.07 seconds**.
- Weighted synthesis real-time factor: **0.732** (compute seconds per output-audio second).
- Synthesis demand: **0.809 compute seconds per input second**.
- Playback demand: **1.105 audio seconds per input second**.

On average, this excerpt fits one synthesis worker's compute budget, with limited margin for bursts, failures and other overhead. It does not fit normal-speed playback within the input duration, even if generation were instantaneous. A steady demand of this size would require at least 1.105× playback speed or shorter faithfully translated speech to prevent continuing backlog. This is an excerpt-specific capacity calculation, not a recommended production tempo or a promise of voice quality.

Average capacity also does not guarantee low latency. Speech arrives in bursts, output lengths vary, complete sentences block first playback, and a four-job cap does not represent a fixed duration budget. In the same full-stack experiment, enabling paced playback reduced completion from 22 to 14 valid jobs while median CPU utilization stayed around 53–54%; all 14 shared completed outputs were byte-identical.

## 3. Queue policy wastes available capacity

The four-job admission cap counts queued, processing, ready and playing jobs together. In the long test, approximately 138 seconds of sampled full-queue conditions had only ready/playing jobs and no pending synthesis. Admission can therefore reject speech while the synthesis worker has no queued job.

Playback duration is checked only after synthesis, and excludes the remaining duration of active playback. Failed or expired jobs with successful synthesis wasted **95.00 seconds of synthesis** and discarded **126.96 seconds of generated audio**. One additional generation failed after **30.40 seconds**, contributing to the longest playback gap. Increasing queue capacity alone would trade rejection for longer delay without fixing excess playback demand.

Translation quality causes separate losses: 19 Chinese-containing outputs, three overlength outputs and six numeral-guard rejections in the long run. Repeated English text increases demand; tiny ASR fragments increase job pressure. These losses should not be labeled hardware overload.

## 4. Fresh TTS thread benchmark

Ran the unchanged PocketTTS worker sequentially at two, one and four configured threads on the Jetson, with ASR, translator, RVC and playback stopped. Each configuration loaded a fresh worker, warmed up once, then generated the same short, medium and long texts twice using the same registered Qiqi reference, five steps and seed 42. The six measured jobs produced exactly 32.16 seconds of audio per configuration.

| Configured threads | Synthesis time for six jobs | Weighted RTF | Median first callback |
| --- | ---: | ---: | ---: |
| 1 | 30.468 s | 0.947 | 2.836 s |
| **2, current default** | **23.289 s** | **0.724** | **2.355 s** |
| 4 | 22.194 s | 0.690 | 2.449 s |

One thread took **30.8% more time** than two. Four took **4.7% less time** than two, with a slightly slower median callback. The six measured outputs were byte-identical across configurations. All 21 WAV hashes, including warmups, were verified locally; samples were finite and no clipping was found. Cold worker load was approximately 1.30 seconds in each configuration.

This small standalone benchmark supports keeping the current two-thread default for now. It does not measure four-thread competition with ASR under the full stack, provide statistical confidence across varied text, or isolate the kernel/memory-bandwidth cause of limited scaling. Production settings were not changed. All benchmark workers exited.

## 5. Hardware envelope and other modes

In the two controlled full-stack probes, median aggregate utilization was 53–54% across six CPU cores, with peaks of 78–81%. Available RAM stayed above 3.35 GiB, swap-in was zero, and maximum CPU temperature was 51.25 °C. GPU median utilization was zero with short peaks up to 99%. PocketTTS uses the CPU provider; translation uses CUDA. Sampled board input power had medians of approximately 9.2 W and peaks around 19.3 W; those samples do not establish a configured power limit or power throttling.

Unused aggregate CPU/GPU capacity does not imply that one autoregressive model can scale proportionally. The thread test demonstrates limited scaling over two threads for these texts. GPU acceleration and memory bandwidth require separate runtime/kernel measurements before assigning a hardware cause.

The 30-minute run had only 1.69 GiB minimum available RAM and no per-process memory trace. Longer-term memory growth remains unassigned. RVC was disabled in these probes. Prior short combined tests verified the RVC path with existing Golden fixtures but recorded some non-startup underruns; they do not establish sustained combined RVC plus translated-speech performance. No new RVC inference/export change or Golden comparison was performed here. Physical microphone/speaker reliability is also outside these paced-null measurements.

## Next experiments in priority order

1. Replace the shared job-count admission policy with a deadline and estimated audio-duration budget, accounting for the active player. Measure coverage together with delay, rather than enlarging the queue.
2. Assemble short ASR fragments into bounded, meaningful units and bound translator repetition while preserving names, numbers and meaning.
3. Deliver generated chunks to a bounded playback buffer and record actual first audible output, underruns, cancellation and ordering.
4. Evaluate concise faithful translations and modest tempo changes with intelligibility and voice-similarity listening. Hardware optimization cannot remove playback duration itself.
5. Repeat the full 30-minute workload after those changes, with per-process memory, per-stage source timestamps and physical output telemetry. Only then test thread/provider changes under full concurrency or decide on a larger board.

## Reusable evidence

- [Stage and capacity measurements](/home/moyamryia/Projects/Project-Mozart/rvc-golden/overload-diagnosis-20261006/performance-budget.json).
- [Thread benchmark summary](/home/moyamryia/Projects/Project-Mozart/rvc-golden/overload-diagnosis-20261006/thread-scaling/summary.json), [benchmark script](/home/moyamryia/Projects/Project-Mozart/rvc-golden/overload-diagnosis-20261006/thread_scaling.py), [collection verification](/home/moyamryia/Projects/Project-Mozart/rvc-golden/overload-diagnosis-20261006/thread-scaling/collection-receipt.json).
- [Controlled comparison and root causes](/home/moyamryia/Projects/Project-Mozart/reports/overload-diagnosis-20261006/RESULTS.md).

The collected thread-benchmark archive SHA-256 is `3ca61ee56a4bb2c11ac943eafc9678bda6196d19f74e2a5908184b38bd3d5cb3`. Saved commands, per-generation measurements, logs, and WAVs are under `rvc-golden/overload-diagnosis-20261006/thread-scaling/` on both hosts. Existing source changes and remote commits were preserved.
