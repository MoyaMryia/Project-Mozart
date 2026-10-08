> 历史记录：本文保留当时的测量、日志与审查原文。当前部署和接口以[维护文档](../../README.md)为准。

# Zero-shot translated-speech redesign — 2026-10-04

Status: investigation and proposed implementation plan, with the initial Jetson TTS benchmark complete. Pocket is the English performance prototype; ZipVoice remains the stronger measured identity comparator. Final engine selection still needs human listening and the user's own references.

Measured follow-up: [Jetson test results](/home/moyamryia/Projects/Project-Mozart/reports/zero-shot-tts-redesign-20261004/test-20261004/RESULTS.md). Pocket used 0.613 GiB peak process RSS and about 3.37 s per warm standard sentence. ASR + Qwen translation + Pocket completed a one-minute run with 4.197 GiB minimum available RAM. Backend integration and optional RVC concurrency remain planned.

Qiqi follow-up: [voice-specific results](/home/moyamryia/Projects/Project-Mozart/reports/zero-shot-tts-redesign-20261004/qiqi-20261004/RESULTS.md). Pocket remained fast/small but Qiqi identity scores were weak; ZipVoice Chinese was promising, while English word accuracy was unreliable. Readiness must be validated per voice, engine and output language. Original clean recordings and confirmed prompt transcripts are required before final engine/profile acceptance; the first benchmark is not a blanket identity pass.

Follow-up: [GitHub alternatives review](/home/moyamryia/Projects/Project-Mozart/reports/zero-shot-tts-redesign-20261004/GITHUB_ALTERNATIVES.md) adds OpenVoice V2 + matching Melo as a third benchmark, and native Qwen3-TTS runtimes as an implementation alternative for the Qwen fallback. C++ speedups are not evidence of lower memory; none of these alternatives was benchmarked on this Nano in that review.

## 1. Product decision

The user confirmed the goal: **speak translated text in a reference voice**.

Build reference-conditioned text-to-speech as a separate speech lane:

```text
mic → preprocessing → ASR final → translation final → speech jobs → clone TTS → playback
                         └────────── subtitle events ───────────────────────→ UI
reference audio + optional exact transcript → Voice Registry ────────────→ clone TTS
optional existing RVC monitor ───────────────────────────────→ playback arbiter
```

The default translated-speech path needs no RVC/Seed-VC conversion after synthesis. This removes a second inference pass, repeated resampling, and dependence on a trained RVC voice for this feature. Source-to-reference speech conversion remains a distinct future capability; it should not define this API or block delivery.

Start with Chinese input and English translated output because that is what the current bridge implements. Make input, reference and output languages separate fields so the architecture can expand later. Reference identity, translated words and source prosody are different properties: this milestone targets identity and intelligible translated words, not preservation of the original speaker's timing/emotion.

## 2. Evidence and baseline

- Local source: `b514273f87e4262aa5191a14fff71e1db61fb7ca`, including merged PRs #1/#2.
- Jetson: `/home/moyamryia/Mozart`, `main` at `fd9a0fc55bc6b84baa16f5d7eb91c0a02eb5cd5b`, two documentation commits ahead of its recorded `origin/main`. Those commits reorganize docs and add Seed-VC research. The relevant runtime source has no diff from local `b514273`; the unpushed commits were preserved without synchronizing or pushing them.
- Existing [Seed-VC investigation snapshot](/home/moyamryia/Projects/Project-Mozart/reports/zero-shot-tts-redesign-20261004/SEEDVC_RESEARCH_20261003.md): offline tiny/base runs succeeded, but recorded total used RAM was 5,370/6,132 MiB. The streaming attempt reached 6,861 MiB used RAM and 840 MiB swap and failed; trusted per-block realtime timing was not established. These are whole-board readings, not isolated model RSS. Seed-VC is already investigated; repeating that work is not the first step.
- Current idle inspection: 7.4 GiB total RAM, approximately 6.0 GiB available and 640 MiB swap already occupied; no matching Mozart/STT/translation/Seed-VC service process was found. Idle headroom does not establish concurrent capacity.
- Installed `.venv` has `sherpa-onnx 1.13.6`, `OfflineTtsZipvoiceModelConfig`, `OfflineTtsPocketModelConfig`, `GenerationConfig` and generation overloads accepting reference audio/text. Installed model folders contain Matcha, Melo, Kokoro and Zipformer; Pocket/ZipVoice cloning weights are not installed there.
- Ordinary RVC ONNX CUDA fallback and model-cache retention remain open from the October 2 audit. The newer Seed-VC report records that microphone capture was subsequently verified; this plan does not make repeating microphone bring-up a prerequisite for TTS research.

## 3. Model decision: shortlist, then measure

| Candidate | Role | Verified upstream support | What remains to prove here |
|---|---|---|---|
| **ZipVoice-Distill int8 through sherpa-onnx** | Preferred first bilingual baseline | Chinese/English release, reference audio plus exact transcript, ONNX and C/C++ examples | Chinese reference → English identity retention, ARM throughput, complete model/vocoder memory, int8 quality |
| **PocketTTS int8 through sherpa-onnx** | English-output challenger | Published English ONNX package, reference audio without transcript, C/C++ examples | Performance on this ARM CPU and fidelity with Chinese reference speech; current upstream multilingual updates do not prove this pinned ONNX package supports other languages |
| **Qwen3-TTS-12Hz-0.6B-Base** | Quality/multilingual fallback if the native shortlist fails | Reference-conditioned cloning and reusable prompt features; multilingual model | Jetson runtime/dependency compatibility, memory and speed with translation; official PyTorch path is not already a Mozart-native implementation |
| Existing Matcha/Melo | Explicit non-cloning fallback | Already deployed fixed-speaker synthesis | Language-correct routing and visible fallback label; never silently claim the selected reference voice |

Sources: [Sherpa ZipVoice](https://k2-fsa.github.io/sherpa/onnx/tts/zipvoice.html), [Sherpa PocketTTS](https://k2-fsa.github.io/sherpa/onnx/tts/pocket.html), [ZipVoice upstream](https://github.com/k2-fsa/ZipVoice), [Qwen3-TTS upstream](https://github.com/QwenLM/Qwen3-TTS).

Benchmark CPU ONNX first to avoid the currently unreliable CUDA EP path and competing with the translation GPU workload. Start with 1/2 threads, then test 4 only if it helps without starving capture/ASR. Test ZipVoice at 4/8 steps and Pocket with its supported release settings; compare quality before retaining a faster setting. Do not use disk size or parameter count as a memory estimate.

Pin source/runtime/checkpoint/tokenizer/vocoder revisions and hashes. Track code and weight licenses separately: Pocket's [model card](https://huggingface.co/kyutai/pocket-tts) currently lists CC-BY-4.0 while its source repository lists MIT; Qwen's [0.6B Base card](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-Base) lists Apache-2.0. Record the exact selected ONNX bundle's accompanying terms. The benchmark does not require blanket claims about model distribution rights.

Selection rule: choose the fastest native candidate that passes cross-language identity/intelligibility and combined-resource gates. Keep ZipVoice as the initial baseline, but let measured English-output results choose Pocket if it is better. Evaluate the Qwen **Base** checkpoint only if neither passes; CustomVoice is a preset-speaker variant and is not an interchangeable reference-cloning checkpoint.

## 4. Required redesign

### A. Separate speaker identity from inference models

Today an RVC `model_id` identifies weights, and [HTTP mode switching](/home/moyamryia/Projects/Project-Mozart/api/src/http_api.cpp:512) even accepts `speaker_id` as a model-ID alias. Introduce `voice_id` for a stored reference profile; a single clone engine can synthesize many voices.

Add a Voice Registry with original reference audio, canonical decoded audio, exact trimmed transcript when required, reference language, selected segment boundaries, revision/hash, engine-specific validation status and a bounded conditioning cache. A profile becomes ready only after a successful preview. Cache decoded/resampled prompts immediately; cache opaque backend conditioning only when the backend exposes/supports it. Do not assume every model can be reduced to one universal speaker embedding.

For ZipVoice, align the transcript to the exact selected audio span. ASR can suggest a transcript, but let the user correct it; the current Chinese-only recognizer cannot reliably transcribe arbitrary English reference clips. Start with manually supplied transcripts. Use model-specific reference lengths rather than a global “three seconds works everywhere” rule: upstream ZipVoice recommends short prompts for speed, while Pocket's example permits a different bound.

Profile files are persistent data. They must not be evicted by the temporary job cache. Deletion should respect active job references; replacing a profile creates a revision rather than changing queued jobs underneath them.

### B. Add a text-oriented speech contract and worker seam

[ModeController](/home/moyamryia/Projects/Project-Mozart/rvc-backend/include/state/mode_controller.hpp:88), [FileRvcWorker](/home/moyamryia/Projects/Project-Mozart/rvc-backend/include/state/file_rvc_worker.hpp:13) and daemon composition currently require `RVCPipelineBase`; the file worker decodes source audio, runs 16 kHz RVC and trims output to source duration. That contract cannot represent text synthesis correctly.

Reuse proven queue lifecycle rules from PR #1, but introduce typed jobs and worker dispatch. Avoid a large rewrite of the verified RVC DSP pipeline. A narrow speech-engine interface should provide capabilities, prepare/release, profile validation, synthesize, cancellation and runtime status. An adapter can use a supervised prototype worker; the preferred production adapter uses the published Sherpa C/C++ APIs once model selection passes.

Proposed endpoints:

- `POST /api/voices`, `GET /api/voices`, `PATCH/DELETE /api/voices/{voice_id}`
- `POST /api/voices/{voice_id}/preview`
- `POST /api/speech/jobs` with `{text, language, voice_id, voice_revision, engine_profile, playback}`
- `GET /api/speech/jobs/{id}`, result download, cancellation and queue controls
- `POST /api/speech/session` to enable/disable translated speech and select voice/language

Return a job ID immediately. Snapshot text, language, reference revision, engine revision and generation settings at enqueue time. Bind generated speech to a stable ASR/translation utterance ID. Preserve atomic result publication, protected unfinished work and cancellation semantics. Do not return partial output as a successful WAV. Do not force TTS duration to equal source-audio duration.

### C. Replace the four-mode matrix with capabilities and resource policy

`RT_ZERO_SHOT`/`FILE_ZERO_SHOT` currently return 501 and refer to audio conversion. Translated speech is an independent sentence pipeline that must keep ASR/subtitles active during synthesis and playback. Do not turn these VC placeholders into ambiguous TTS modes.

Keep legacy RVC modes compatible; expose independent speech-session state and capabilities such as `reference_tts`, `sentence_playback`, `streaming_tts` and supported languages. Add a resource manager that admits workers according to measured CPU/RAM/GPU limits. Default translated-clone operation should leave unnecessary RVC engines unloaded. Permit CPU clone TTS plus realtime RVC only after the combined benchmark passes; a heavy GPU alternative needs an explicit suspend/reject policy.

Make model prepare/unload asynchronous and observable. The [daemon](/home/moyamryia/Projects/Project-Mozart/state/src/daemon.cpp:58) currently constructs RVC eagerly; the controller holds a non-owning pipeline reference and [model manager](/home/moyamryia/Projects/Project-Mozart/rvc-backend/src/rvc/model_loader.cpp:226) caches loaded voices. Introduce explicit ownership/release and bounded caches so idle/unload actually means released inference resources. Keep long model loads off the controller/status lock. Replace speculative `cudaDeviceReset` and “3–6 second swap” documentation with measured lifecycle behavior; device reset in a shared native process can invalidate other CUDA users there. A supervised worker process exit is a possible isolation boundary when deterministic resource release is needed.

### D. Replace demo playback with one owned output path

[The subtitle bridge](/home/moyamryia/Projects/Project-Mozart/tools/subtitle_bridge.py:51) has an unbounded list, ignored `aplay` failures and hardcoded `plughw:1,3`. [The TTS-to-RVC demo](/home/moyamryia/Projects/Project-Mozart/tools/tts2rvc.py:1) injects synthesized samples into the RVC UDP route, whose reply is bound to the first client. That is unsuitable as the main cloned-speech transport.

Create one output owner with a configured named ALSA device, a bounded queue measured in audio seconds, stop/flush behavior and a documented priority/ducking policy for optional RVC monitoring. Resample from the engine's declared rate to the playback contract once. Keep generated speech metadata separate from captured mic timestamps; never feed generated output back into the ASR input tap.

For the first version, complete a sentence to a WAV, then play it. Add model-supported chunk playback later. Advertise first-chunk streaming only after callback cadence, gaps and cancellation have been measured. Keep speaker leakage/echo handling an explicit playback policy for a speaker-based demo; headphones can validate synthesis and routing first.

### E. Make the translation bridge a reliable producer

Consume final, stable translation events only; display subtitles independently of synthesis success. Add utterance IDs, deduplication and final-event replay recovery. Use a bounded FIFO for archival jobs and a bounded-age live queue that explicitly skips stale speech rather than accumulating minutes of delay. Translator failure must not synthesize Chinese text through an English-only engine.

Supervise ASR, translation and speech worker lifecycles; add timeouts, cancellation and child cleanup. Fix the previously reproduced subtitle-log rotation gap or move internal events to a structured channel with browser SSE as its gateway. Keep the translation model warm with its bounded context when the measured budget permits, and keep a single selected TTS engine warm rather than reloading per sentence.

### F. Build a task-oriented UI

Add a Reference Voices panel: upload, transcript correction, selected segment, language, preview, ready/error status and delete. Add a Translated Speech panel: voice selection, output language, queue age, synthesis/playback state, mute/stop and source/translated text. Separate RVC models from reference voices and engine-specific controls; pitch/index/protect sliders do not belong in clone TTS.

Expose selected runtime, model revision, memory/queue pressure and fallback identity. Fix the hidden live panel while sharing status plumbing. A service failure should leave captions visible and make speech degradation clear.

## 5. Delivery order and exit gates

| Phase | Deliverable | Exit gate |
|---|---|---|
| 1 — Benchmark before integration | Fixed text/reference fixture matrix, pinned ZipVoice/Pocket runners, comparison recordings, resource traces and engine decision | Chinese reference → English speech passes listening and text checks; selected engine fits the actual budget; reported native/upstream differences are explained |
| 2 — Reference voice preview | Voice Registry, text synthesis API, typed jobs, isolated worker/native adapter and result download | Multiple references produce distinguishable stable voices; revisions are immutable per job; cancellation/restart/deletion work; existing RVC queue tests pass |
| 3 — Translated sentence playback | Final translation events → speech jobs → one playback owner; UI voice/stop/status | Stable event order, no duplicated sentences, bounded queue age, captions survive speech failures, selected named device works |
| 4 — Resource and recovery hardening | Lazy RVC lifetime, measured admission, bounded caches, supervised processes and event recovery | 30-minute combined run meets memory/latency targets, no swap growth or unbounded queues, mode/profile swaps recover without leaked models |
| 5 — Optional streaming/optimization | Warm prompt reuse, native C++ adapter if prototype used, verified chunks and tuned threads/steps | Actual first-audio/cancellation improvements with no material quality regression; capabilities updated only for proven support |

Implementation units: model probe/fixtures first; Voice Registry and API types second; worker/resource seam third; playback/bridge fourth; UI and integration validation fifth. Each can be reviewed separately. Seed-VC live conversion, new RVC retrieval tuning and wholesale ONNX export rewrites are outside the critical path.

## 6. Validation contract

Create reusable fixtures under `rvc-golden/clone-tts/`: at least three reference voices, Chinese/English reference variants, short/long sentences, numbers/names and translated text with a changed word order. Reuse exact audio and text; record hashes, prompt transcript/crop, versions, RNG/settings, sample rates and generated outputs. Use clean human reference samples for identity; espeak can supply deterministic input to the separate ASR/translation baseline. Synthetic references alone cannot prove convincing human cloning.

Run original upstream TTS first, then the selected ONNX/Sherpa/native adapter on the same fixtures. Capture conditioning/intermediates where available. Use matched randomness when comparing exports; int8 or independently random generation needs declared numerical tolerances plus intelligibility/identity evaluation, not an unjustified exact waveform test. The project's existing RVC Golden procedure remains required for any RVC change; RVC tensor names and T=200 are not a TTS input contract.

Measure cold load, profile preparation, warm synthesis, first playable audio, audio duration/RTF, callback gaps, CPU utilization, peak process RSS/PSS and whole-board available memory/swap. Measure end-to-end timestamps separately: source segment end, ASR final, translation final, synthesis admission/start, first audio ready and first physical playback. “RTF < 1” alone is insufficient for a pleasant sentence-level experience.

Proposed interactive gates, **not measured results**:

- Keep at least 1 GiB `MemAvailable` in the intended combined workload; no sustained swap growth. Record the already occupied swap baseline rather than claiming zero swap.
- Warm synthesis RTF ≤0.8; target translation-final → first audible output p95 ≤2 seconds on the fixed short-sentence set. An engine passing offline quality but failing this latency remains a file-preview option, not an advertised interactive voice.
- Bound queued live playback to approximately 5 seconds and expire stale utterances; exact policy becomes configuration after usability testing.
- No missing/duplicated core words on the hand-audited short set; log WER/CER on a larger held-out set with a suitable target-language ASR. Speaker similarity scores support paired listening; no universal embedding cutoff substitutes for audition.
- Finite output, no unintended clipping, no obvious seam discontinuities; cancellation and worker recovery have documented bounds.
- Optional simultaneous realtime RVC retains its deadline/counter behavior in the combined test. If it fails, reject that combination or suspend it explicitly.

Tune quantization, steps, threads, prompt length and warm caches only after correctness is established. This investigation loaded no new model, installed no package, changed no runtime source and made no new audio-quality claim. The next concrete work is Phase 1, not implementing a Seed-VC worker first.
