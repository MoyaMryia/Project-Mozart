# Mozart full project run — 5 October 2026

**The selected complete stack fits the 8 GB Jetson, but the current project does not yet deliver reliable translated speech in a cloned voice. The largest blockers are segmentation, recognition/translation accuracy, speech-job integration and voice acceptance.**

This is an actual run on `jetson-wg`, not a memory estimate. Production inference assets, the native preprocessor, backend, streaming ASR, subtitle bridge, Qwen server, TTS models, frontend and hardware audio interfaces were exercised. The cloned-TTS worker is a test-only adapter: it consumes actual subtitle-bridge translations, but is not an implemented Mozart zero-shot backend worker.

## Coverage and results

| Component | Result | Evidence and practical limit |
|---|---|---|
| Native build | Pass | Current Jetson `build-gpu` runtime was up to date. No tracked runtime changes. |
| Native regression checks | Pass | 12 backend/state checks, 2 preprocessor checks and 2 I/O checks. The replay entry contains four Python cases. These host checks use test inference, not the production neural models. |
| Frontend | Builds and renders | TypeScript check and production build passed. Correctly launched production preview rendered and polled live Jetson status. Voice-library control remains disabled; live waveform section is hidden by `showLive=false`. |
| Golden foundation | Pass | Locked checkpoint, official HuBERT/RMVPE assets, source commit, runtime and reference outputs verified. Existing captured Golden tensors were reused; this was not a new original-PyTorch inference. |
| ONNX Generator parity | Pass | Identical captured tensors: matching float32 shape `[1,1,1142400]`, finite output, cosine **0.9999997403**, maximum absolute error **0.0018689**. This establishes the tested case, not arbitrary dynamic-length support. |
| Real file API | Correct but slow | Multipart upload → queue → processing → completed → WAV download worked. **80.73 s for 21.8 s of speech**, approximately RTF **3.70**. CUDA EP failed for RMVPE, HuBERT and Generator with `cudaErrorNoKernelImageForDevice`; sessions rebuilt on CPU. |
| File audio versus Golden | Pass | Correlation **0.99999970**, median F0 difference **0 cents**, RMS difference **0.00096 dB**, output **21.8 s**. |
| Clean realtime TensorRT | Works | Espeak **1,191/1,191** replies; natural source **1,601/1,601**. Zero loss, inference errors, late blocks or resets. Only **15 startup underrun frames = 300 ms**. |
| Realtime versus upstream Python | Close on tested source | After the reported startup trim and duration crop: 30 s versus 30 s, log-mel correlation **0.98294**, RMS-envelope correlation **0.98544**, median F0 difference **20 cents**, zero clipping. |
| Preprocessor + RVC + ASR + Qwen + TTS + frontend | Runs together | Four 45 s paced-input phases: Melo, Pocket and ZipVoice with RNNoise disabled for the clean processing comparison; Pocket with default RNNoise enabled. Every phase returned **2,250/2,250** UDP packets with no inference errors or late blocks. |
| Default RNNoise/text path | Better segmentation, remaining errors | Complete utterances reduced 61 fragments to 6 subtitle events; all 6 Pocket jobs generated, with no queue drops. Recognition and translation remained inconsistent across repeated identical input. |
| Default translated-speech engine | Fails English control | Actual Chinese Matcha service ignored every English word as out of vocabulary and produced approximately 0.1 s of audio. Chinese control generated 2.2 s of audio in 0.70 s. |
| Pocket clone prototype | Fits, promising for sentences | Default-RNNoise phase median generation **5.63 s**, median first callback **4.26 s**, median RTF **0.707**. Two sampled complete outputs had 0 automatic WER against the translated text. Qiqi identity is not approved. |
| Short-text clone behavior | Failure found | Pocket generated **24.88 s** for the one-word text “Wuhan”; Whisper transcribed “Well, I…”. Coalescing text and rejecting abnormal jobs are necessary. |
| Subtitle SSE | Append works; rotation fails | A fresh client received an ordinary append, then failed to receive an append after the JSONL file was replaced. |
| Hardware audio | Interfaces work; drain needs attention | Five-second real USB microphone → RNNoise → RVC → USB playback run: **250/250** replies, no inference errors, **249 packets played and one ALSA underrun**; capture was nearly silent. A generated clone WAV also played through the stable USB ALSA device with exit code 0. Human hearing/voice identity and live speech recognition were not verified. |
| Zero-shot backend modes | Unimplemented | Both `rt_zero_shot` and `file_zero_shot` returned **HTTP 501**. Test TTS generation does not change that product capability. |

## Concurrent capacity and output behavior

All concurrent phases used the Qiqi split TensorRT realtime profile, Chinese Zipformer ASR on two CPU threads, Qwen 0.8B Q4_K_M on GPU with context 2,048, the real subtitle bridge and a served frontend. Native playback ran through ALSA's `null` device for long file replays. Separate short checks exercised physical USB capture/playback. TTS was generated and saved during each phase, rather than streamed into physical playback live.

| 45 s phase | RNNoise | Subtitle events | Generated speech jobs | Jobs dropped by test queue | Minimum available RAM | RVC resets / underrun frames |
|---|---:|---:|---:|---:|---:|---:|
| Melo VITS, CPU 2 threads | Off | 61 | 31 | 30 | 2.326 GiB | 156 / 1,047 |
| Pocket, CPU 2 threads | Off | 61 | 12 | 49 | 2.001 GiB | 156 / 1,046 |
| ZipVoice, CPU 4 threads, 4 steps | Off | 61 | 11 | 50 | 2.023 GiB | 156 / 1,054 |
| Pocket, CPU 2 threads | On | 6 | 6 | 0 | **1.987 GiB** | 12 / 200 |

Peak system unavailable memory was approximately **5.38 GiB** out of 7.37 GiB usable. No OOM or 768 MiB resource-guard stop occurred. Swap was already in use before the run: the first three phases increased its maximum use by about **197 MiB**; the later RNNoise run increased it by about **41 MiB** relative to its own baseline. This is a bounded capacity result, not a long-duration stability guarantee. Alternative large models such as Seed-VC were not loaded into these phases.

The test speech queue held one running request plus at most two waiting requests, dropping the oldest waiting request when full. This bounded policy is part of the harness, not deployed product behavior. The production subtitle speaker uses an unbounded Python list. Queue-drop counts show why translating and speaking each tiny fragment cannot be the final design.

Long file replays logged **2,249 played packets for 2,250 sends**, despite the proxy receiving all 2,250 backend replies. The native preprocessor stops its playback thread immediately at the send limit; the final reply is therefore not played at that boundary. Add an explicit output-drain phase before stopping playback. The single physical ALSA underrun also prevents calling the hardware path glitch-free.

Reported API/packet ages around 20–31 ms measure packet metadata/return timing. **They are not the latency of the buffered converted audio.** The clean realtime startup was 300 ms. Pocket's first callback is also an application callback, not sound reaching a physical speaker. Complete translated utterances still require endpoint detection, translation and several seconds of speech generation.

## Confirmed first divergence: segment metadata

`StreamingRvc::push` treats any change in `segment_id` as a stream discontinuity and clears its context/output state. Both the ASR service and RVC consume segment metadata intended for speech boundaries. The RNNoise-disabled energy fallback changes segments frequently within ordinary speech; even the default RNNoise path changes IDs at utterance boundaries.

A controlled, paired 15 s experiment used **identical captured preprocessor PCM**, identical RNNoise, the same model, source, pacing and inference parameters. Only byte 19, the RVC-bound segment ID, was held constant in a UDP test proxy. No C++ implementation or model asset changed.

| Condition | RVC resets | Total underrun frames | Startup underrun frames | UDP replies |
|---|---:|---:|---:|---:|
| Original segment metadata | 4 | 63 | 17 | 750/750 |
| Only RVC-bound segment ID held constant | **0** | **15** | **15** | 750/750 |

This identifies segment-triggered resets as the cause of the additional gaps in the tested path. A production fix should distinguish audio transport discontinuities from ASR utterance boundaries, preserving realtime context across continuous frame/PTS input. It should retain resets for genuine dropped-frame or clock discontinuities. The proxy is a diagnostic, not a proposed permanent routing workaround.

## Text and voice quality

The fixed human Chinese source says, approximately: “That was thirty-six years ago, in 1987. I got into the computer science department at Wuhan University.” With RNNoise disabled, `1987` was split into requests such as “one,” “ninety-eight,” and “seven,” so the resulting translations were unusable as a sentence.

Default RNNoise produced complete utterances, but repeated runs changed `那还是` into `那孩子` (“that child”). Qwen then introduced claims about a child being born or graduating. Even an utterance with a correct year could lose the “thirty-six years ago” clause. The median full-utterance translation request took **801.5 ms**; low latency does not establish translation faithfulness.

Automatic Whisper checks of selected generated audio:

| Output | Intended text / observed issue | Result |
|---|---|---|
| Melo English sentence | “That was thirty-six years ago.” | Transcribed “as there is tates”; 100% WER. |
| Pocket English sentence | Same text | Transcribed “That was 36 years ago.” Strict token WER is 33.3% because digits differ from word spelling; this example preserves the sentence's apparent meaning. |
| Pocket short fragment | “Wuhan” | 24.88 s output, “Well, I…”; 200% strict WER. |
| ZipVoice English sentence | “That was thirty-six years ago.” | Transcribed “That was six years ago”; 16.7% WER, meaning changed. |
| Pocket default-RNNoise sentences 1 and 2 | Actual complete English translations | 0% WER against those texts. This tests reading the text, not correctness of the preceding ASR/translation. |

These are small automatic checks, not a human listening acceptance test. Reference voice conditioning used the previously saved Qiqi original-PyTorch RVC output crop, not a clean original Qiqi actor/game recording. The earlier Qiqi identity concerns therefore remain open. Preserve a per-voice, per-engine, per-output-language acceptance gate.

## Next work, in order

1. **Separate transport continuity from utterance segmentation.** Fix the confirmed RVC reset behavior first; give the RNNoise-disabled VAD path real hangover/hysteresis. Retest the same frozen input, then the original metadata, against the verified inference path. Acceptance: no non-startup underruns on continuous frames, genuine discontinuities still reset safely, and Golden/parity checks remain unchanged.
2. **Improve the text path before approving speech output.** Validate recognizer endpoint/connection state, quantify repeat-run drift, coalesce short fragments, and evaluate a stronger ASR option within the measured memory budget. Require faithful handling of names, years and quantities; translation must not invent subject or chronology. Use multiple speakers and languages rather than this single clip.
3. **Implement translated speech jobs and voice profiles.** Replace the Chinese-only default English reader. Keep reference audio/transcript, engine, target language and preview approval in a voice registry. Add bounded queues, cancellation, stale-job policy, maximum output duration and explicit failures. Pocket is an English performance prototype; clean-reference Qiqi English identity must pass listening checks before deployment.
4. **Integrate and expose the real capability.** Implement backend TTS/zero-shot workers, wire callback audio to a bounded playback lane, select stable ALSA devices, drain output at shutdown, and surface truthful capabilities/errors in the frontend. Restore live controls and remove placeholder voice counts. Fix SSE inode replacement and supervise STT/translation/TTS child shutdown.
5. **Optimize after correctness.** Rebuild ONNX CUDA kernels for Jetson sm_87 or validate a suitable TensorRT file profile; avoid silently paying CPU fallback cost. Load quality/realtime assets on demand. Measure first audible speech, backlog, thermals and swap activity under a 30–60 minute multilingual soak, including mic and speaker interaction. Keep at least 1.5 GiB operational headroom as an initial target.

RVC index parsing regression checks pass, but production index loading remains intentionally disabled in `model_loader.cpp`. Enabling retrieval requires its own quality/memory check; moving the index-rate slider currently cannot establish that retrieval is active.

## Evidence, audition and reproduction

All test evidence is under [full-run-20261005](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005). **All 172 packaged files passed SHA-256 verification after copying locally**, including the audio and telemetry. The main files are [concurrent results](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/results.json), [RNNoise comparison](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/followup-results.json), [metadata bisection](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/segment-bisect.json), [RVC quality metrics](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/rvc-quality.json), [speech scoring](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/asr-evaluation.json), and [artifact hashes](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/artifact-manifest.json).

Listen to the original Chinese source, RVC result and translated clone:

- [Chinese source](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/audition/original-chinese-input.wav)
- [Qiqi RVC full-stack excerpt](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/audition/qiqi-rvc-full-stack.wav)
- [Qiqi conditioning reference](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/audition/qiqi-reference-voice.wav)
- [Actual translated English clone](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/audition/qiqi-english-full-stack.wav)
- [Short-word failure](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/audition/qiqi-pocket-short-word-failure.wav)

RVC reproducible input/Golden/ONNX/backend side by side:

- [Original espeak input](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/audition/espeak-input.wav)
- [Full Python Golden](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/audition/espeak-python-golden.wav)
- [Captured-tensor Python Generator](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/audition/espeak-captured-golden-generator.wav)
- [Identical-tensor ONNX Generator](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/audition/espeak-onnx-generator.wav)
- [Actual file API backend](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/audition/espeak-mozart-file-backend.wav)
- [Upstream Python realtime](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/audition/natural-upstream-realtime.wav) and [fresh TensorRT realtime](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/audition/natural-mozart-realtime.wav)

Captured Generator WAVs include the captured padded sequence and are 23.8 s; full file-pipeline WAVs are 21.8 s. Compare Generator parity to its captured-tensor Python counterpart. Audition copies are PCM16; only files exceeding full scale receive documented listening-only gain. Raw measured float WAVs are untouched.

Remote artifacts remain in `/home/moyamryia/Mozart/rvc-golden/full-run-20261005`. Test scripts use existing Jetson assets and save isolated configuration/storage. Run Golden verification and ONNX parity before the main workload; run `followup.py`, `file_check.py`, `ui_smoke.py`, `segment_bisect.py`, then offline `evaluate.py` and `render_audio.py` sequentially. Do not score with Whisper during timed workloads. The initial baseline-helper attempt used the ASR/TTS virtual environment, which lacked librosa; it was rerun successfully in the Golden virtual environment. An initial incorrectly launched dev frontend was also replaced by a correctly launched production preview. Neither attempt is counted as a product failure.

The first follow-up SSE reader expired during an idle interval; its subsequent false append results are superseded by the fresh-connection `ui_smoke.py` check. STT shutdown logs included broken-pipe errors during test teardown, so graceful child-process shutdown still needs attention; they are not counted as inference failures.

Local source is `b514273f87e4262aa5191a14fff71e1db61fb7ca`; remote source is `fd9a0fc55bc6b84baa16f5d7eb91c0a02eb5cd5b`, retaining its two existing documentation commits. No tracked application code, model assets, power settings or Git history were changed. All owned services and the temporary SSH tunnel were stopped.
