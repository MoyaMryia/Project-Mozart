> 历史记录：本文保留当时的测量、日志与审查原文。当前部署和接口以[维护文档](../../README.md)为准。

# Mozart Jetson overload diagnosis — 2026-10-06

The demonstrated bottleneck is the speech playback/admission design. The Jetson retained CPU and RAM headroom in two controlled probes, yet enabling paced playback caused valid speech to be dropped. Translation defects and occasional stalled generation cause additional losses. These results support redesigning the speech pipeline before changing hardware.

## Controlled comparison

Replayed seconds 720–840 of `/tmp/test-audio.mp4` at microphone pace on `jetson-wg`, using the same Qiqi reference, PocketTTS int8 CPU worker, two configured threads, five steps, seed 42, Zipformer plus SenseVoice final refinement, and Qwen 0.8B Q4 CUDA translation. RVC was disabled. Each probe used a fresh supervised stack; only the playback setting differed. Paced ALSA null output waited for actual generated duration.

| Measurement | Paced playback | WAV-only generation |
| --- | ---: | ---: |
| Input duration | 120 s | 120 s |
| Native frames / overruns | 6,000 / 0 | 6,000 / 0 |
| ASR finals | 24 | 24 |
| Valid translated jobs completed | 14 / 22 | 22 / 22 |
| Admission queue rejections | 6 | 0 |
| Playback queue failures / expired jobs | 1 / 1 | 0 / 0 |
| Overlength / untranslated-text rejections | 1 / 1 | 1 / 1 |
| Completed audio duration | 105.52 s | 132.56 s |
| Median synthesis real-time factor | 0.738 | 0.735 |
| Median aggregate CPU use, six cores | 54.1% | 53.4% |
| Peak aggregate CPU use | 78.1% | 81.4% |
| Minimum available RAM | 3.35 GiB | 3.41 GiB |
| Swap-in pages | 0 | 0 |
| Maximum CPU temperature | 51.0 °C | 51.25 °C |
| Supervisor exit status | 0 | 0 |

All 24 source texts were identical. The 14 jobs completed in both modes also had identical translated text, duration, and WAV SHA-256 hashes. This makes the completion difference attributable to enabling playback in this experiment, rather than different recognition or generated audio. The same two invalid translations were rejected in both modes.

CPU headroom persisted during input feeding: median aggregate use was 54.1% and 53.1%; restricting to feeding samples where TTS was busy gave 57.0% and 56.2%. The CPU worker used approximately 2.4 core equivalents, despite two configured model threads; multiple runtime pools can contribute. Median worker RSS was about 0.61 GiB. RSS includes shared mappings and is not exclusive memory.

GPU utilization had a median of 0%, with brief peaks of 99%/96%. PocketTTS is explicitly configured for CPU inference; the translator uses CUDA. This does not establish that a CUDA PocketTTS conversion would be straightforward or faster. The probes recorded small swap-outs (37/188 pages), but no swap-in activity. There was no observed high-temperature slowdown in these probes.

## Why playback loses speech

1. **Admission couples synthesis to playback.** The four-job cap counts every nonterminal job, including ready and playing jobs. Consequently, audio waiting to play can reject a new synthesis request even when the synthesis worker has no work. See [speech_service.py](/home/moyamryia/Projects/Project-Mozart/tools/speech_service.py:157). In the saved 30-minute test, 69 of 280 samples with all four slots occupied had only ready/playing jobs, with no queued or processing job. At approximately two seconds per sample, this represents roughly 138 seconds of this condition, not an exact time integral.

2. **Playback capacity is checked after synthesis.** The worker finishes a whole WAV before checking whether waiting audio exceeds 12 seconds. That check excludes the remaining duration of the currently playing job. See [speech_service.py](/home/moyamryia/Projects/Project-Mozart/tools/speech_service.py:295). In the 30-minute test, failed/expired jobs with successful synthesis consumed 95.0 seconds of compute and produced 126.96 seconds of discarded audio. This excludes the separate generation failure.

3. **There is no incremental playback.** The engine callback records timing and bounds duration; generated chunks are not sent to the player. Playback waits for the complete WAV. See [clone_worker.py](/home/moyamryia/Projects/Project-Mozart/tools/clone_worker.py:59). A first-callback metric is therefore not first audible speech latency.

4. **Output sometimes exceeds the available listening time.** The 22 valid jobs generated 132.56 seconds of speech from 120 seconds of input: 10.5% more playback time than input time. Even instantaneous synthesis cannot play that excerpt at normal speed within its input duration. This is measured for the excerpt, not an estimate of demand for the entire recording. Bursts and fragmented utterances make a job-count limit particularly restrictive.

5. **Stalls create additional gaps.** Completed playbacks in the 30-minute run had 449.4 seconds of intervening gaps in total. The largest gap was 41.48 seconds. One generation ran for 30.40 seconds before failing its duration guard; 29.13 seconds of that attempt overlapped the largest gap, followed by worker reload overhead. Playback is not continuously busy, so its duration mismatch alone does not explain every skip. Gaps also do not prove the CPU was idle.

The 30-minute test completed 244 of 407 caption finals. It recorded 105 admission queue rejections, 24 playback queue failures, five expirations, one generation-duration failure, 19 translations containing Chinese, three overlength translations, and six translation numeral-guard rejections. These sum to the 163 noncompleted finals. The 28 translation/validation losses are separate from queue pressure. Some overlength outputs were repeated English phrases, reaching 1,243 characters. Of 401 captions with translated text, 75 produced only one to three English words; 37 of those had admission errors. Short ASR fragments increase request pressure and can damage translation context.

## Repair completed during diagnosis

Fixed a separate quick-restart defect in [run_translated_speech.py](/home/moyamryia/Projects/Project-Mozart/tools/run_translated_speech.py:23). TCP port preflight now uses `SO_REUSEADDR` and verifies `listen()`, permitting reuse after closed connections while still rejecting an active listener. UDP exclusivity is unchanged.

The three OS-level regression checks in [test_runner_ports.py](/home/moyamryia/Projects/Project-Mozart/tools/tests/test_runner_ports.py:10) passed both locally and on the Jetson: active TCP rejection, TCP TIME_WAIT reuse, and active UDP rejection. The initial WAV-only startup failure was archived under `wav-only-startup-attempt`; only that unsuccessful mode was retried. This restart defect did not cause the original 30-minute playback overload. No inference settings, native pipeline, queue policies, hardware settings, or existing commits were changed for the comparison.

## Recommended redesign order

1. **Admit by estimated audio duration and deadline before synthesis.** Include remaining active playback, waiting audio, and estimated queued synthesis. Track synthesis and playback capacity separately, while retaining one overall latency budget. Report distinct admission, expiry, generation, and playback outcomes. Validate with the same excerpt, then the 30-minute replay; compare coverage and latency together.
2. **Assemble meaningful translation units.** Merge tiny ASR fragments with a bounded wait, preserve source ordering and numbers, and prevent runaway translation repetition. Reject or repair untranslated spans before requesting TTS. Avoid reducing coverage merely to improve the completion percentage.
3. **Deliver incremental TTS audio to a bounded player buffer.** Measure first audible output, underruns, cancellation, continuity, and sentence order. The current callback needs an actual chunk transport and player; changing the status label alone is insufficient.
4. **Manage output duration without losing meaning.** Test concise faithful translation and a modest tempo adjustment, assessing intelligibility and perceived voice similarity. Larger queues alone trade drops for stale speech when output demand exceeds playback capacity.
5. **Profile remaining synthesis costs after the queue changes.** Compare thread counts and runtime options under the complete workload. Track per-process memory over a full run and investigate failed or repeating generation. Consider another backend or GPU work only against these measured results.

## Evidence and limits

- [Comparison summary](/home/moyamryia/Projects/Project-Mozart/rvc-golden/overload-diagnosis-20261006/summary.json), [identical-output verification](/home/moyamryia/Projects/Project-Mozart/rvc-golden/overload-diagnosis-20261006/comparison-verification.json), [active-feed CPU measurements](/home/moyamryia/Projects/Project-Mozart/rvc-golden/overload-diagnosis-20261006/feed-resource-metrics.json).
- [Saved long-run analysis](/home/moyamryia/Projects/Project-Mozart/rvc-golden/overload-diagnosis-20261006/saved-run-analysis.json), [queue coupling samples](/home/moyamryia/Projects/Project-Mozart/rvc-golden/overload-diagnosis-20261006/queue-coupling.json), [original 30-minute report](/home/moyamryia/Projects/Project-Mozart/reports/long-speech-20261006/RESULTS.md).
- Reusable profiler, per-second process/resource samples, tegrastats, jobs, captions, launcher logs and WAVs are under `rvc-golden/overload-diagnosis-20261006/` on both hosts. Collected archive SHA-256: `a96a0b94d08b6095a9f6f718a661a80ed740305dd8334abbe05f4e70251673ed`.

The earlier 30-minute run had a minimum of 1.69 GiB available RAM but lacked CPU/GPU and per-process memory telemetry. The short probes cannot rule out longer-term memory growth or identify the cause of that lower minimum. Memory bandwidth was not measured. Paced null output checks scheduling and duration, not physical speaker behavior. No new human voice-similarity judgment or RVC Golden/ONNX test is claimed. Both probes finished and their owned services were confirmed stopped; the completed long-test follow-up remains paused.
