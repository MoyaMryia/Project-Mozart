# Mozart next-steps audit — 2026-10-02

## Follow-up: PR #1 merged

After this audit, the user authorized merging PR #1. Its description was updated with the Jetson results, it was marked ready, and GitHub merged it at `b514273f87e4262aa5191a14fff71e1db61fb7ca` on 2026-10-02. Both local and Jetson main checkouts were fast-forwarded to that commit. The production Jetson `build-gpu` rebuild passed, followed by 12/12 native tests and 4/4 paced replay cases. Logs: `pr1-main-build.log`, `pr1-main-native-tests.log`, `pr1-main-replay-tests.log`.

The audit below records the pre-merge snapshot. Its physical demo, runtime, subtitle and UI follow-ups remain applicable; the draft/merge work in priority 3 is now complete.

## Decision

The verified qiqi inference path is numerically sound. The next milestone should be a reproducible physical realtime demo, followed by supervised subtitles. Prioritize explicit model/device selection, runtime visibility and memory control before adding more concurrent models. The FAISS parser fix does **not** currently enable production retrieval.

This audit inspected `/home/moyamryia/Mozart` through `ssh jetson-wg`, the source, deployed assets, current GitHub PRs, frontend build, audio configuration and actual backend inference. Main is `0fb19e22f3ffe5424c2ebf6c163f9b84a7b15939`, the merge of PR #2. PR #1 is still an open draft at `8f153199a7d72d4aeaf0dcb2b45bcc1bd5054dd5`.

## What passed

| Check | Result and scope |
|---|---|
| Production main | PR #2 merged; local and Jetson main agree; production GPU build and 5 backend tests passed during the preceding review. |
| Draft PR #1 combined with current main | Clean integration in an isolated checkout; ONNX/CUDA EP/TensorRT build passed; 12/12 native tests, 4/4 paced replay tests, and 70/70 repeated lifecycle cases passed. This integration was tested without committing or pushing it. |
| Frontend | `vue-tsc --noEmit` and Vite production build passed with the installed NVM Node runtime. |
| Existing Golden | Input/checkpoint/official assets/source/runtime/output hashes and stored quality checks passed `verify_golden.py`. This validates the existing reference; it is not a new PyTorch generation or fresh human audition. |
| Captured Golden → ONNX Generator | Identical captured inputs; float32 outputs `[1,1,1142400]`, all finite. Cosine 0.9999997403; mean absolute error 0.0000227454; maximum absolute error 0.00186891. Passed declared cosine ≥0.999 and max error <0.01. |
| Real FILE_RVC | Exact retained espeak input, deterministic qiqi full model: correlation 1.0000, F0 difference 0 cents, RMS difference approximately 0 dB. |
| Real legacy UDP streaming | Correlation 0.9999995232, F0 difference 0 cents, RMS +0.000911 dB. All 1,191 packets received, including 100 flush packets; zero inference errors, late blocks, resets or input/output overruns. |
| USB capture configuration | Current mono PCM2902 capture opens with `plughw:CARD=Device,DEV=0` under Mozart's 48k/S16/stereo contract. Direct `hw` rejects the stereo request. No samples were read. |
| Subtitle SSE | Appending a new line delivers an event. Replacing the file and appending to its replacement produces no event: recovery gap confirmed. |
| Speech assets | Deployed Zipformer recognizer initializes; archived CUDA llama-server executable starts its version command; Chinese and English Matcha assets exist. Full concurrent recognition/translation/speech inference was not run. |

The Generator comparison used the case-specific qiqi full export at **T=2380**. It does not establish dynamic sequence support or change the current standard Generator T=200 contract. The legacy streaming test has 129 initial underrun frames (2.58 seconds); all underruns were at startup. It does not reproduce the accepted split realtime profile's approximately 320 ms first output. The UDP test uses preprocessed contract frames and bypasses physical capture/RNNoise/VAD.

## Priority and acceptance criteria

### 1. Make physical realtime startup deterministic — demo prerequisite

Observed startup from the retained backend configuration selected `qiqi-zh-full`, with `has_realtime_generator=false`. Factory selection follows directory iteration and the first usable model. Selecting realtime mode alone therefore does not guarantee the accepted split profile. No Mozart/subtitle/LLM service was running during the initial inspection, and no matching service units were installed.

Add an explicit initial model setting or a launcher that activates `qiqi-zh-realtime` before capture begins. Use stable ALSA names rather than card indices. The connected adapter is now USB `08bb:2902`, PCM2902/C-Media, card `Device`; its capture stream is mono S16_LE, 44.1/48 kHz. The previously tested HK MIC is absent. Existing `plughw` configuration can handle channel adaptation, so the raw stereo rejection does not by itself require changing capture code.

Acceptance: one command starts the intended model, backend and capture/playback; health reports the active split profile and actual engine assets; a retained paced input passes first, then the actual mic/cable/output path is auditioned. Measure first audible output, steady latency, ALSA overruns and stream counters separately. Repeat start/stop and mode changes with clean shutdown. The external `mozart-pre` process currently requires explicit orchestration.

### 2. Bound runtime memory and resolve ordinary ONNX CUDA fallback — concurrency prerequisite

The real full-file test encountered `cudaErrorNoKernelImageForDevice` on RMVPE, HuBERT and the qiqi full Generator, then rebuilt their ONNX sessions on CPU. Neighboring fixed TensorRT engines did load on GPU; full-length inputs required fallback beyond those fixed shapes. Compiling CUDA EP support is insufficient evidence that these graphs execute on the Jetson GPU.

During this cold file job, available memory fell to about **467 MiB**, with about **566 MiB swap** used. The 21.8-second input took roughly 83 seconds including cold setup/fallback, not a steady throughput benchmark. After test daemons exited, available memory recovered to about 4.5 GiB, with 455 MiB still swapped. Existing older low-occupancy concurrency numbers remain scoped to those earlier assets and cannot establish current full-path headroom.

Inspect the deployed ORT/CUDA dependency builds for the target sm87 architecture and verify real kernels, or use suitable separately verified TensorRT assets for each required shape. Add actual backend/asset/fallback information to status. `ModelManager` retains loaded models in its map and feature initialization loads both quality and realtime assets; idle mode stops work without necessarily releasing these engines. Introduce a bounded cache and mode-specific lazy loading with safe lifetime handling for in-flight work.

Acceptance: repeat the same reference case after any runtime change; demonstrate intended GPU execution, numerical equivalence, and bounded memory across repeated model and mode changes. Then measure the realtime split profile with ASR, Q4 translation (`-c 2048`, thinking disabled) and optional TTS under paced load. Record late blocks, warm/cold latency, peak shared memory and swap growth.

### 3. Complete draft PR #1 — small, validated queue improvement

PR: https://github.com/MoyaMryia/Project-Mozart/pull/1 — `fix: preserve queued work and add paced realtime validation`.

The reviewed changes protect queued/running/cancelling jobs from capacity, clear-finished and cache-eviction errors, and add paced preprocessor replay. The tested combination with current main passed the checks above. GitHub reports the draft as mergeable. No new blocking defect was found in this audit. Its playback metric is correctly called packet age; it must not be reported as complete microphone-to-audible-output latency.

Next: refresh its branch onto current main, retain the integration test evidence and complete paced preprocessor → split realtime backend validation with the deployed RNNoise asset. Physical playback remains a separate acceptance check. Promote and merge when its draft workflow is complete; this audit did not change its draft state or merge it.

### 4. Supervise the subtitle lane and fix SSE recovery — second demo milestone

`tools/subtitle_bridge.py` launches STT without a `finally` cleanup, signal forwarding or restart supervision. Translation is synchronous while consuming child stdout; production needs explicit buffering/backpressure and restart/error behavior. SSE in `api/src/http_api.cpp` keeps reading the opened file descriptor and does not detect replacement/truncation; the replacement probe reproduced stalled subtitles.

Add a defined launch/shutdown/health contract for STT, translator and bridge. Detect file inode changes and truncation, or replace file tailing with a defined event channel. Bound pending translation work and expose degraded status when the translator is unavailable. The current Python tools are a useful demo stage; DESIGN.md's eventual zero-Python runtime requires a deliberate later implementation choice.

Acceptance: subtitles continue after producer restart, log replacement/truncation and browser reconnection; translator failures preserve source text; shutdown leaves no child processes; sustained input cannot grow queues indefinitely.

### 5. Expose usable realtime UI controls — parallel product task

`frontend/src/App.vue` defines `showLive=false` with no enabling assignment. The hidden panel contains working mic-mute/bypass actions; waveform canvases have no data connection. Model management remains disabled. The production build passes, but that does not establish usability.

Expose mode-dependent live controls and selected profile/runtime/device status first. Define bounded meter/waveform telemetry before connecting canvases. Clearly distinguish a profile's realtime capability from generic RT_RVC support. Keep unsupported zero-shot capabilities hidden until implemented.

Acceptance: selecting the intended realtime voice is explicit; mute/bypass state matches the API across mode changes; the live panel and meters update; disconnected/degraded service states are visible.

### 6. Enable retrieval only after a separate Golden comparison

The repaired parser loads deployed qiqi English/Chinese and mike_tyson indices. However, `rvc-backend/src/rvc/model_loader.cpp:134–141` still deliberately disables `load_index()`, and the inferencer skips unloaded indices. Thus **changing `index_rate` currently has no production effect**.

Implement guarded/lazy activation and distinguish index file existence, successful load and active retrieval in status. Avoid unconditional extra resident memory. Compare captured HuBERT queries/retrieved features with the corresponding Python FAISS/RVC path before a backend audio A/B. Match nprobe, neighbor aggregation and mixing semantics explicitly; parser correctness alone does not establish RVC retrieval equivalence. `de_narrator.index` still requires the training-side asset.

Acceptance: rate 0 reproduces the current baseline; nonzero rate demonstrably uses the intended index and matches its reference retrieval tensors; failure is explicit; memory remains bounded; retain paired audio for audition.

### 7. Correct optional TTS language, routing and queue behavior

The bridge defaults to Chinese Matcha while `--lang` defaults to reading English translation. Existing documentation already records that the Chinese lexicon cannot read English. Playback is hardcoded to `plughw:1,3`, errors are discarded, and the speech queue is unbounded. The current HDMI card is named `HDA`, but numerical card order is not a stable routing contract.

Select a model appropriate to the requested output language and a configurable named playback device. Bound/drop stale speech deliberately, surface synth/playback failures, and establish shared output ownership/mixing when RVC and TTS coexist.

Acceptance: the chosen language is audibly intelligible, output reaches the intended physical connector, playback failures are visible and queues stay bounded during sustained speech. Add TTS to the concurrency test only after realtime and subtitles remain stable.

### 8. Expand voices/exports after the demo path is reliable

The two de_narrator exports tested during PR #2 review still fail at T=50/100 and contain internal random operators. Their cross-export error cannot justify replacing an asset. The qiqi result in this audit does not prove de_narrator correctness.

For de_narrator, reuse an appropriate deterministic espeak input, establish its own original PyTorch audible Golden, capture tensors, and export shared explicit noise or a declared deterministic path before ONNX/backend comparison. Only claim shapes actually tested. For additional low-latency voices, validate each split export and metadata independently.

PipeWire implementation and zero-shot are later enhancements. ALSA plus UDP already provides a physical route to validate. Keep the USB gadget route closed under the recorded hardware decision.

## Documentation corrections

Update TODO.md/DESIGN.md alongside the corresponding work: distinguish parser verification from activated retrieval; GPU compilation from actual kernel execution; idle mode from released engines; old HK MIC tests from the current mono adapter; fixed case-specific exports from proven dynamic shape support; and earlier benchmark assumptions from current measurements. The blanket “compute and memory are plentiful” conclusion needs its original workload scope.

## Audio and evidence

Exact input SHA256: `e93d38e7e44a86bbb88f19f073ce21f791bfc2f959e6257554d853fe87d99c51`. All fresh backend tests used the same retained espeak WAV with pitch 0, index rate 0, RMS mix 1, protect 0.33, RMVPE, and file RNNoise/VAD disabled.

- [Input](/home/moyamryia/Projects/Project-Mozart/rvc-golden/next-steps-audit-20261002/qiqi-espeak-pinyin-zh-en-mixed-input.wav)
- [Existing audible Python Golden](/home/moyamryia/Projects/Project-Mozart/rvc-golden/output/qiqi-zh-espeak-pinyin-mixed-python-reference.wav)
- [Existing deterministic Python Golden](/home/moyamryia/Projects/Project-Mozart/rvc-golden/output/qiqi-zh-espeak-pinyin-mixed-python-reference-DET.wav)
- [Captured Golden Generator, including padding](/home/moyamryia/Projects/Project-Mozart/rvc-golden/next-steps-audit-20261002/qiqi-captured-golden-generator.wav)
- [ONNX Generator, same captured tensors and padding](/home/moyamryia/Projects/Project-Mozart/rvc-golden/next-steps-audit-20261002/qiqi-captured-onnx-generator.wav)
- [Fresh file backend output](/home/moyamryia/Projects/Project-Mozart/rvc-golden/next-steps-audit-20261002/verify_offline-deterministic.wav)
- [Existing deterministic streaming Golden](/home/moyamryia/Projects/Project-Mozart/rvc-golden/output/qiqi-zh-espeak-pinyin-mixed-streaming-python-reference-DET.wav)
- [Fresh legacy UDP backend output](/home/moyamryia/Projects/Project-Mozart/rvc-golden/next-steps-audit-20261002/stream-legacy-direct.wav)

Raw Generator outputs are 23.8 seconds including padding; the file output is 21.8 seconds. UDP output is 23.82 seconds including flush/startup handling. Fresh outputs are finite with no clipped samples. See `audio-statistics.json` for RMS, peak and spectral centroid; compare corresponding paths rather than unlike padded durations.

Evidence: `golden-verification.log`, `golden-onnx-generator.json`, `backend-verification.log`, `backend-daemon.log`, `stream-direct.log`, `stream-legacy-comparison.json`, `pr1-build.log`, `pr1-queue-repeat.log`, `capture-configuration.log`, `subtitle-rotation-probe.log` and startup JSON files. Probe sources are saved here. The initial combined backend verifier exited during UDP setup because `.venv` lacks librosa; its FILE_RVC case passed. UDP was then rerun using the existing `vc_backend_venv`, producing the successful result above. No dependency installation was necessary. A post-inference NumPy JSON serialization error in the audit harness was corrected by converting metric values; it did not invalidate the saved audio.

No new human audition, ambient microphone recording, physical playback, fresh split-profile latency measurement, full concurrent stack test or de_narrator Golden was performed. No model assets or production configuration were replaced. All audit daemons were stopped. Only audit artifacts were added to the main checkout; PR #1 remains draft.
