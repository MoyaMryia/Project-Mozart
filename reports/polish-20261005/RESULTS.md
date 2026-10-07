# Mozart integration polish and Jetson results — 2026-10-05

Mozart now has a working reference-conditioned English TTS backend using Kyutai PocketTTS through sherpa-onnx. The native API, Vue controls, source captions, translation bridge, persistent voice registry, supervised worker and bounded playback queue are integrated. It is a usable development prototype. Recognition, translation and cross-language voice identity have not passed a daily-use quality bar.

## What changed

- Uploaded 3–15-second reference WAVs are validated and stored as immutable, hashed profiles. The UI can select a voice, generate/listen/download previews, enable translated speech, cancel jobs and stop the queue. Preview availability does not mean human identity approval.
- Native `/api/voices` and `/api/speech/*` proxy to an isolated, warm PocketTTS worker. Jobs expose real lifecycle states, errors and timing. Missing service returns 503 while native routes remain healthy. Completed downloads and utterance deduplication survive restart.
- Queue capacity is four unfinished jobs, with 12 seconds of waiting playback audio and a 30-second live deadline. One output owner overlaps synthesis with playback. Full queues, excessive duration, nonfinite audio and playback failures are explicit; captions continue.
- Ordinary TTS peaks receive measured scalar gain before PCM16 encoding. Gross amplitude remains rejected. The previously rejected peak sentence now completes without clipping. RVC audio processing was not altered to compensate for neural mismatches.
- `tools/run_translated_speech.py` owns and cleans up native API, ASR, Qwen, TTS and native microphone/replay processes. `test-audio.mp4` is decoded to PCM and sent through the real paced native preprocessing/UDP path. USB capture/output use named ALSA devices.
- Zipformer endpoints replace ASR segment-ID word chopping. Optional SenseVoice refines final PCM spans. Source text, engine, refinement timing and translation/speech errors are retained. Numeral guards retry once, then withhold suspect speech; they do not validate semantic accuracy.
- RVC no longer resets continuous audio on ASR segment changes. Frame gaps and real clock discontinuities still reset. Energy-only VAD now initializes its noise floor correctly, uses hangover, and preserves short pauses. Finite replay drains its output before shutdown.
- `state/translated-speech.yaml` disables unused RVC capabilities and neural engine construction. UI hardware labels, decimal controls, active speech configuration, subtitle deduplication and speech-only layout were corrected; dead controls were removed.

## Replayed workloads

The input is the user's audio-only `test-audio.mp4`, 4,185.84 seconds long (about 70 minutes), SHA-256 `2b80f2d78c59c530b53cb96e8a8aef9852f169231f312a67ce352a19db61b389`. The remote `/tmp/test-audio.mp4` matched. We replayed 14 minutes across three runs, covering 12 unique minutes. This is not a full 70-minute or physical-speaker soak.

| Run | Native input | Caption finals | Completed / failed jobs | Minimum available RAM | Median TTS synthesis RTF |
| --- | --- | --- | --- | --- | --- |
| Initial combined stack, seconds 0–120 | 6,000 frames, zero overruns | 29 | 28 / 0 | 1.84 GiB | 0.754 |
| Combined stack, seconds 120–720 | 30,000 frames, zero overruns | 124 | 107 / 1 | 1.21 GiB | 0.747 |
| Speech-only + SenseVoice, seconds 0–120 | 6,000 frames, zero overruns | 29 | 19 / 1 | 2.84 GiB | 0.736 |

RTF is synthesis seconds divided by generated audio seconds. It excludes endpoint waiting, translation, queue waiting and physical playback. Median enqueue-to-audio-ready was 5.55 / 7.52 / 3.91 seconds respectively; this is not capture-to-speaker latency. Median translation time was 406 / 447 / 419 ms.

The initial run generated WAVs without playback. The ten-minute run loaded RVC assets and used ALSA `null` before null pacing was corrected; it exercised synthesis and output calls but cannot establish real playback throughput. The final two-minute run paced `null` to audio duration and exposed overload honestly: six 429 admissions, one unsupported-text/language admission, two withheld numeral-check translations, and one generated sentence skipped because waiting playback exceeded 12 seconds. Nineteen completed jobs produced 85.76 seconds of audio.

The ten-minute run had seven 429 admissions, three 422 admissions, one 400 admission, four rejected numeral-check translations and one former amplitude rejection. Its 107 completed jobs produced 620.64 seconds of audio. The exact amplitude rejection was fixed and separately retested; the ten-minute workload was not rerun after that fix. Across runs, unfinished work never exceeded four. System swap was already in use: observed usage ranged approximately 0.77–0.92 GiB on combined runs, and about 0.80 GiB in the speech-only run. Available-memory measurements do not prove swap-free operation or causally assign all swap to Mozart.

## Validation

- **17 native CTest entries and 16 Python lifecycle/translation tests passed.** Jetson native builds, Vue type checking and Vite production build passed. Final daemon rebuild and disabled-RVC smoke check passed without neural engine construction.
- API checks passed for unsupported languages, malformed request shapes, invalid references, in-use reference deletion, unused fixture deletion, deduplication, cancellation, recovery, unavailable worker and restart/download preservation.
- SSE passed append, inode replacement and incomplete-line handling. UI deduplication uses stable utterance IDs. The old test-owned subtitle file was restored after rotation checks.
- Real frontend Generate Preview reached completed state and exposed a WAV download with 3.0 seconds audio / 2.4 seconds synthesis. Browser file-chooser automation stalled; **upload via the UI was not verified**. The upload API passed with the actual Qiqi WAV. Screenshot below records completed frontend generation.
- The short Qiqi preview played on `plughw:CARD=Device,DEV=0` with `aplay` exit 0. No human confirmed sound, pronunciation or identity. Sustained output was null-based.
- Active cancellation returned in 3.9 ms and the next preview completed after worker reload. That real cancellation happened near worker startup, so it is not an inference-interrupt latency measurement. Fake-worker lifecycle tests also exercise active synthesis cancellation.
- A one-word `Wuhan` synthesis exceeded its four-second duration allowance and was rejected. The duration guard contains the failure; it does not fix the model's short-input behavior.

## RVC evidence and limits

The existing deterministic espeak fixture and locked Golden artifacts were verified first, following AGENTS.md. Original PyTorch Golden inference was **not rerun** in this pass. Fresh ONNX Generator inference used identical captured tensors: shapes/dtypes matched, values finite, cosine 0.9999997403, maximum absolute error 0.00186891 and matching 23.8-second output. This validates the tested captured length; it does not advertise new dynamic axes or change Mozart's fixed T=200 contract. Runtime assets retain TensorRT `.engine` precedence.

The current backend then processed the same espeak fixture concurrently with the live workload: 1,191/1,191 contract replies, 99 blocks, zero inference errors, resets, overruns and late blocks. There were 18 output underruns: 16 startup and **two additional**. A second 15-second native preprocessor test crossed real segment IDs [0,1,2]: 750/750 replies, zero resets/errors/overruns/late blocks; 18 underruns comprised 15 startup and **three additional**. Native ALSA-null playback returned all 750 frames with no reported hardware underrun. Packet return age excludes buffered audio and is not acoustic latency.

The severe segment-change reset regression is fixed. Occasional concurrent RVC gaps remain; this pass does not claim complete realtime quality acceptance.

| Audition | Artifact |
| --- | --- |
| Deterministic original input | [espeak input](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/audition/espeak-input.wav) |
| Known Golden output | [Python Golden](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/audition/espeak-python-golden.wav) |
| ONNX comparison output | [ONNX Generator](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/audition/espeak-onnx-generator.wav) |
| Current concurrent backend output | [Mozart backend](/home/moyamryia/Projects/Project-Mozart/rvc-golden/polish-20261005/espeak-qiqi-backend-concurrent.wav) |
| Original video excerpt | [first 15 seconds](/home/moyamryia/Projects/Project-Mozart/rvc-golden/polish-20261005/video-microphone-first-15s.wav) |
| Qiqi reference | [reference WAV](/home/moyamryia/Projects/Project-Mozart/rvc-golden/full-run-20261005/audition/qiqi-reference-voice.wav) |
| Actual translated video speech | [translated WAV](/home/moyamryia/Projects/Project-Mozart/rvc-golden/polish-20261005/qiqi-translated-video.wav) |
| English preview | [preview WAV](/home/moyamryia/Projects/Project-Mozart/rvc-golden/polish-20261005/qiqi-english-preview.wav) |
| Former peak failure, corrected encoding | [retested WAV](/home/moyamryia/Projects/Project-Mozart/rvc-golden/polish-20261005/qiqi-english-peak-fixed.wav) |

## Recognition, translation and clone quality

The optional official SenseVoice int8 model decoded the saved 6.06-second Mandarin fixture with digit-normalized CER 0, cold load 2.42 seconds, warm decoding about 0.48 seconds (RTF 0.079), and peak process RSS about 0.61 GiB. This is **one fixed fixture**, not a general accuracy claim. Live endpoints still sometimes divide words (for example 我肯 / 定), so the option remains experimental.

Offline cached Whisper-small checked three TTS WAVs after timed workloads exited. The translated clip `Sister, this is my little story.` had WER 0. The preview had WER 25%, including omitted Hello and Qiqi recognized as Cheechy. The longer retest had WER 15.8%, including an extra repetition of `it is not a problem`. These are small automatic intelligibility samples, not human voice-similarity approval. Qiqi's Chinese reference producing English speech is still unaccepted for identity.

Live source ASR and Qwen 0.8B translations still have clear semantic errors. An example from the recorded long run translated `月的增火味然后经过这么一分期一个月一百六` as `The increase in fire flavor followed by this period of one hundred and sixty days.` Numeral checks cannot correct that. Preserve original captions and do not call current translations reliable.

## Next changes, in order

1. Repair final-utterance PCM alignment and word seams. Evaluate the same annotated Mandarin excerpts with Zipformer and optional SenseVoice; require word-boundary and numeral accuracy before choosing a default.
2. Replace or improve the translator based on a small fixed bilingual evaluation set covering names, money, years, negation and colloquial speech. Measure memory jointly on Jetson. Keep failed translations visible and withheld from speech.
3. Reduce sentence-to-playback delay and overload. Test shorter safe sentence units, duration-aware admission and a genuinely streaming reference-TTS path. Require a paced 30–60-minute run with bounded delay and a stated skip budget before claiming sustained live interpretation.
4. Audition Qiqi alongside the input reference, validate pronunciation and identity with human ratings, and handle short-word/repetition failures. Compare another reference-TTS engine only if it improves these measured outcomes within the RAM budget.
5. Investigate the remaining non-startup RVC underruns under the combined workload using unchanged Golden fixtures. Then verify the physical microphone/speaker path and long-run shutdown/recovery.

## Run it

On `jetson-wg`, from `/home/moyamryia/Mozart`:

```bash
.venv/bin/python tools/run_translated_speech.py \
  --backend-config state/translated-speech.yaml \
  --reference rvc-golden/clone-tts/qiqi-20261004/reference-qiqi-natural-source.wav \
  --reference-name zh-qiqi \
  --input /tmp/test-audio.mp4 --seconds 120
```

Omit `--input` for the USB microphone. Use `--no-playback` for WAV-only output. The frontend runs separately (`cd frontend && npm run dev -- --host 0.0.0.0`). See [deployment and API](/home/moyamryia/Projects/Project-Mozart/tools/REFERENCE_SPEECH.md) for the optional final model and independent speech service.

Changes are in the local checkout and deployed Jetson checkout. Existing Jetson documentation commits were preserved; README/API additions were appended there. No new commit, push or PR was created in this polish pass. Raw results, telemetry, scripts and audition files are under [test artifacts](/home/moyamryia/Projects/Project-Mozart/rvc-golden/polish-20261005/summary.json), with [lifecycle checks](/home/moyamryia/Projects/Project-Mozart/rvc-golden/polish-20261005/post-checks.json), [RVC segment check](/home/moyamryia/Projects/Project-Mozart/rvc-golden/polish-20261005/rvc-check/results.json), [intelligibility checks](/home/moyamryia/Projects/Project-Mozart/rvc-golden/polish-20261005/tts-intelligibility.json) and source SHA-256 verification. All 25 deployed runtime source files and 174 downloaded WAV/result artifacts matched byte for byte. Test-owned services and the browser/tunnel were stopped after verification.

![Completed reference preview UI](/home/moyamryia/Projects/Project-Mozart/rvc-golden/polish-20261005/control-ui.jpg)
