# Translated reference speech

Reference speech synthesizes translated text in a voice from a reference WAV.
The current engine is [Kyutai PocketTTS](https://github.com/kyutai-labs/pocket-tts), through the [sherpa-onnx reference audio API](https://k2-fsa.github.io/sherpa/onnx/tts/pocket.html).
The recorded English bundle is `sherpa-onnx-pocket-tts-int8-2026-01-26`.
The tested runtime is sherpa-onnx 1.13.6.

RVC conversion uses a different pipeline.
The reserved `rt_zero_shot` and `file_zero_shot` conversion modes still return HTTP 501.
Reference speech uses the routes in this document.
The [writing rules](../docs/WRITING.md) define the project terms.

## Start the system

The native build, Python environment, and installed ASR, translator, and TTS assets are necessary for this procedure.
The [deployment guide](../frontend/DEPLOYMENT.md) gives the native build procedure.
The supervisor uses `build-gpu/state/mozart_stated`.

On the configured Jetson, start a paced file replay:

```bash
.venv/bin/python tools/run_translated_speech.py \
  --backend-config state/translated-speech.yaml \
  --reference reference.wav --reference-name "My voice" \
  --input test-audio.mp4 --seconds 60
```

For microphone capture, omit `--input`.
For a ten-minute excerpt, add `--start 120 --seconds 600`.
For generation without ALSA playback, add `--no-playback`.
The capture path does not mix synthesized output into the captured PCM.
A physical microphone can still receive sound from a nearby speaker.

The default named audio device is `plughw:CARD=Device,DEV=0`.
The command options can change the capture and playback devices.
The translated-speech configuration disables RVC to avoid unused engine loads.
Simultaneous RVC deployment must have its own configuration and memory test.

For a demo without physical audio, add these options:

```text
--input test-audio.mp4 --start 720 --seconds 120 --playback-device null --keep-open
```

The input uses microphone pace.
The null output waits for the actual duration of each generated piece.
After capture and queued speech end, the API and voice previews stay available.
The supervisor continues its child-process checks and 768 MiB available-memory guard.

The supervisor starts the native API, ASR, translator, and speech service.
It rejects occupied ports. Capture starts after the native API and online ASR become ready.
Speech engine warmup and reference selection run in a background thread.
During model startup, translation can fail and speech can skip. The bridge reports these outcomes without replaying earlier utterances.
Translation and speech service failures keep source captions running.
Native API or ASR failures still stop the stack. The 768 MiB available-memory guard remains active.
With the default coverage policy, the supervisor waits for queued translation and speech after file input ends.
An explicit stop still ends capture and playback. Waiting tasks remain on disk.
It does not start the frontend or optional RVC monitor.

In a different terminal, start the frontend:

```bash
npm --prefix frontend run dev
```

### CUDA speech

The speech service accepts `--provider cuda` and `--precision float32`.
The supervisor uses `--tts-provider cuda` and `--tts-precision float32`.
The default settings remain `cpu` and `int8`.
CUDA speech needs a compatible sherpa ONNX Runtime library and the float32 Pocket assets.

For an independent CUDA runtime directory, add these supervisor options:

```text
--model /path/to/sherpa-onnx-pocket-tts-2026-01-26
--tts-provider cuda --tts-precision float32
--tts-pythonpath /path/to/gpu-runtime/python
```

The directory must contain `sherpa_onnx` and its necessary shared libraries.
The supervisor sets this Python path only for the speech service.
The ASR and translator keep their existing environments.

The worker reports `provider_requested`, `precision`, `onnxruntime_library`, `onnxruntime_version`, and `available_providers` in the speech status `runtime` object.
The `onnxruntime_library` field identifies the C API implementation library.
The `onnxruntime_loader_library` field identifies the library that the sherpa binding loaded.
These paths can differ when a compatibility interface sends C API calls to another library.
They do not prove GPU kernel execution or satisfactory performance.
If the loaded runtime has no CUDA provider, a CUDA request fails before model initialization.
The worker does not silently change that request to CPU.

For ONNX Runtime diagnostics, add `--tts-provider-config /path/to/provider.conf`.
An independent service uses `--provider-config` for the same file.
The file uses the [sherpa session configuration](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.6/sherpa-onnx/csrc/session.cc) format.
For example:

```text
LogSeverityLevel=0
ProfilingFilePrefix=/absolute/path/to/profile
```

Profiling and verbose logs add work to inference.
Use a separate run for performance measurements.

The [dated CUDA setup script](../rvc-golden/realtime-patches-20261008/install_cuda_overlay.py) creates `gpu-runtime/run-realtime.sh` after runtime compilation.
This launcher supplies the installed CUDA paths and float32 settings.
Use the launcher with the usual reference and capture options.

The [2026-10-08 Orin report](../reports/realtime-patches-20261008/GPU.md) contains the measured results and test conditions.
In that test, CUDA float32 took more time than the existing CPU int8 configuration.
Keep CPU int8 as the default until another test shows a benefit.

### Speech threads

The supervisor accepts `--tts-threads`. The default value is 2.
This option changes only the speech worker. The translator keeps its existing thread settings.
The [continuity report](../reports/realtime-patches-20261008/CONTINUITY.md) compares one, two, three, and four speech threads on Orin.
Four threads reduced first-piece synthesis time in that test. The complete playback delay did not clearly decrease.
Use `--tts-threads 4` for another test with CPU int8.

## Translation memory settings

The supervisor limits the prompt-state cache to 128 MiB.
The logical and physical batch sizes are 256 and 128.
The recorded translator runtime had an 8 GiB default cache limit.
These limits prevent that default from determining the deployment memory budget.

The options are `--translation-cache-mib`, `--translation-batch-size`, and `--translation-ubatch-size`.
The `--llama-model` option selects a different model.
The default translator is Qwen 0.8B.
Use this model for the next latency test. Evaluate larger models only when specific translation errors require them.

## Reference profiles and independent service

The frontend accepts reference WAV files of 3–15 seconds.
The sample rate range is 8–48 kHz; mono and stereo are accepted.
The maximum upload size is 5 MiB.
The panel can make previews, select the live voice, and stop queued speech.
A satisfactory preview shows synthesis completion.
Voice similarity and pronunciation checks must include listening.

For independent service management, start the worker:

```bash
.venv/bin/python tools/speech_service.py \
  --model ~/models/sherpa-onnx/clone-test-downloads/sherpa-onnx-pocket-tts-int8-2026-01-26 \
  --playback-device plughw:CARD=Device,DEV=0
```

The native API proxies speech requests to loopback port 18081.
The `MOZART_SPEECH_PORT` variable changes that port.
An missing service returns HTTP 503.

For diagnostic capture, add `--archive-utterances` to the supervisor command.
This option saves processed 16 kHz PCM16 source excerpts, hashes, and timestamps.
It is off by default.
The original input is the source reference.

## Translation and ASR limits

The bridge uses `--speak` for speech requests.
It uses the selected speech session and accepts only translated text for speech.
A failed translation publishes the source caption and an error.
It does not use source-language speech as a substitute.
Partial Zipformer captions reach the frontend before endpoint detection.
One background worker translates final utterances in order.
The default coverage policy stores waiting final utterances in a disk queue.
The bridge keeps only 128 recent caption records in memory. It retrieves older waiting records from disk.
With `--delivery-policy realtime`, four final utterances can wait behind the active request.
For that policy, a full queue produces an explicit skipped translation.
The subtitle revision increases for each update; fixed revision numbers do not identify processing stages.

### Numeric checks

Explicit digits, spoken four-digit years, and large quantities receive numeric checks.
A failed check permits one fresh translation retry.
The bridge limits each attempt to 128 output tokens.
A response with `finish_reason=length` fails the translation checks.
Repeated quantities must keep their occurrence counts.
The checks reject invented large amounts, missing quantities, untranslated Chinese, and token-limit truncation.
A calendar month name can replace its number only if it does not hide another missing quantity.

The bridge normalizes exact standalone 万/亿 quantities and ranges before translation.
It converts exact composites such as 七万五千 to 75,000 as one quantity.
It does not accept two quantities, 70,000 and 5,000, as an equivalent translation.
Vague, malformed, mixed, and colloquial quantities stay unchanged.
The caption audit keeps the original Chinese and attempted English.
These checks do not prove semantic accuracy.

### Recognition uncertainty

Optional SenseVoice refinement runs in one background worker.
The online Zipformer result starts translation before refinement finishes.
Two utterances can wait behind the active refinement task.
A full queue, initialization failure, or shutdown deadline produces an explicit refinement status.

SenseVoice uses a second decode for explicit amounts or years of at least 100.
Both decodes use the same PCM and one loaded model.
The second decode uses the literal-number mode.
Conflicting values produce `refinement_numeric_audit`; strict 可能/不可能 conflicts produce `refinement_polarity_audit`.
These later audits do not cancel or replace speech from the online result.
The frontend shows a different refinement separately. It does not replace the original translation source.
Agreement between readings does not prove transcript accuracy.

External final records with `asr_numeric_audit` or `asr_polarity_audit` still use the existing rejection rules.
Those rules can prevent translation and speech when the supplied audit contains an issue.

Two reproduced boundary forms produce `asr_boundary_audit` and `speech_skipped_reason=asr_boundary_uncertainty`.
The forms are a clause ending after 不管自己…在 and the ambiguous repeated mother-doubt subject.
The bridge keeps the source caption and sends no translation or speech request.
These specific warnings do not give a general grammar check.
Blocked captions count as unspoken source coverage.

General endpoint merging is disabled.
Fixed-input experiments recovered some words but combined amounts and added excessive wait.
Specific translation constraints keep explicit 他不管 in third person.
They also reject an added “herself” in the measured mother-doubt construction without 自己.
The attempt audit gives `explicit_role_issues` and `source_role_constraints`.

These constraints do not prove full role or meaning accuracy.
The local benchmark is `rvc-golden/sentence-context-20261007`.
It contains continuous input, automatic drafts, and a review page.
Automatic drafts are not human-confirmed references.
The [source review report](../reports/sentence-context-20261007/RESULTS.md) gives the confirmed scope.

## API

| Request | Contract |
| --- | --- |
| `GET /api/speech/status` | Returns engine, tested output languages, session, profiles, jobs, and queue limits. |
| `POST /api/voices` | Accepts `{name,audio_base64,language,transcript}` with base64 WAV bytes. |
| `GET /api/voices` | Lists stored reference profiles and SHA-256 revisions. |
| `DELETE /api/voices/{id}` | Refuses deletion while an unfinished job uses the reference. |
| `POST /api/speech/jobs` | Accepts `{text,language,voice_id,playback?,utterance_id?,live?}` and queues a job. |
| `GET /api/speech/jobs/{id}` | Returns the logical job state. |
| `GET /api/speech/jobs/{id}/result` | Returns only a completed WAV. |
| `DELETE /api/speech/jobs/{id}` | Cancels the job and active synthesis or playback. |
| `POST /api/speech/session` | Accepts `{enabled,voice_id,language,playback}` for translated speech. |
| `POST /api/speech/events` | Accepts final translation `{text,utterance_id}` from the bridge. |
| `POST /api/speech/stop` | Disables live admission and cancels unfinished jobs. |

Job states are `queued`, `processing`, `ready`, `playing`, `completed`, `failed`, `cancelled`, and `expired`.
Duplicate utterance IDs return the previous logical job.
The service keeps up to 128 finished logical results.
With the coverage policy, waiting tasks remain in the disk queue until processing or explicit cancellation.
The status contains recent jobs and active jobs. It does not list every waiting task.
Use `unfinished_jobs` and `pending_jobs` for the full queue counts.
Use the job route to retrieve an older waiting job by ID.

## Admission and deadlines

The default `--delivery-policy coverage` gives coverage priority over playback delay.
The supervisor sends this policy to both the caption bridge and the speech service.
Waiting speech does not expire after 30 seconds. Queue delay and estimated duration do not cause HTTP 429 refusal.
The service accepts waiting text before audio capacity becomes available.
The existing 12-second audio buffer limit still controls synthesis. The service keeps generated WAV files on disk.
Delay can increase when the output duration exceeds the input duration.
Invalid text, translation checks, engine errors, and explicit cancellation can still prevent speech.

Speech tasks use `speech-jobs.sqlite3` in the speech data directory.
Translation tasks use `captions/translation-queue.sqlite3` in that directory.
SQLite commits waiting tasks before the service returns success. The service keeps a limited cache of speech records.
JSON result files remain available as snapshots. SQLite remains the task source if a JSON snapshot write fails.
The `storage_warning` status field reports that failure.
A task storage failure returns HTTP 507. Existing stored tasks remain available.
Only one process can own each disk queue.
The system can still use its configured swap. The application does not change system swap settings.

For the previous delay limits, select `--delivery-policy realtime`.
The following limits apply only to that policy.

Live playback uses an estimated 24-second audio budget.
This budget includes the remainder of active playback.
Download-only requests can have four unfinished synthesis jobs.
Live playback admission does not apply that four-job limit to ready or playing jobs.

When the audio budget is full, up to four text jobs can wait in FIFO order.
They reserve no audio and start no synthesis until capacity is available.
Their initial 30-second deadline stays in effect.
A full text queue or an excessively large single job returns HTTP 429.
Captions report this refusal.
The fields `admission_pending`, `was_deferred`, and `admission_wait_seconds` describe the wait.

Live playback jobs expire after 30 seconds if playback has not started.
Download-only jobs apply this deadline to the first ready piece.
After playback starts, the job finishes its ordered pieces.
The projected first-play time includes earlier deferred text.
Admission rejects a sentence whose projected start exceeds its deadline.
Such refusal counts as unspoken coverage.

Engine load and coverage piece synthesis have a 90-second timeout.
With the realtime policy, live piece synthesis has a 20-second timeout.
These limits detect failed engine requests. They do not limit time in the coverage queue.
Cancellation terminates active synthesis or playback and removes queued pieces.

## Pieces and playback

Live or played text uses sentence, clause, and word boundaries.
The target is 12 English length units or 24 Chinese characters per piece.
An ordinary English word counts as one unit.
A three-digit numeral counts as two; a numeral with four or more digits counts as four.
The splitter can use up to 15 units to avoid a short remainder or finish a nearby ordinary-word sentence.

A leading numeric clause can split after three written words because speech expands the numeral.
The length units select boundaries; they do not change the admission estimate or audio limits.
Adjacent same clauses with at least three words use different pieces when necessary.
The splitter keeps intentional repetitions and all translated words.
It keeps existing clause boundaries before extending a short sentence remainder.
Numeric ranges count both endpoints.

Each piece is a full WAV.
Playback can start while later pieces synthesize.
The engine does not produce a token audio stream.
A completed logical job has one downloadable WAV with the pieces in order.

Played or live pieces have a 12-second generated duration limit.
Non-live download-only generation has a 45-second limit.
The worker rejects empty, nonfinite, unsafe-amplitude, or excessively long output.
For ordinary peaks, it applies a measured scalar gain before PCM16 encoding.
Each job reports the raw peak and output gain.
Failed output is not a satisfactory preview.

One synthesis worker operates with one ALSA playback owner.
Buffered audio includes the active playback remainder and has a 12-second limit.
Generation waits before the next piece when the estimated buffer is full.
An unexpectedly long completed piece waits until its actual duration fits.
The service does not discard that piece to increase throughput.
ALSA null playback uses actual audio duration for queue tests.

## Synthesis controls and storage

PocketTTS input receives terminal punctuation and combined ellipses.
All original words and numbers stay unchanged.
Each piece records the adjusted `synthesis_text`.

Live and multiple-piece jobs set seed 42 and `max_frames` through the full `GenerationConfig.extra` map.
The native map getter returns a copy; an in-place update does not set the frame limit.
Output that reaches this limit has an unverified end.
The worker rejects that output.

Reference profiles use `~/.local/share/mozart/speech/voices`.
Completed jobs use the adjacent `results` directory.
With the coverage policy, a restart resumes waiting tasks and tasks that have not started playback.
The service reports interrupted playback as failed. It does not automatically repeat audio that might have reached the speaker.
An explicit stop cancels queued speech. Normal service shutdown keeps waiting tasks for a later start.
Completed downloads remain available within the history limit.
With the realtime policy, a restart marks interrupted jobs as failed.
A new profile is necessary for a different engine.

The optional `--engine zipvoice --vocoder PATH` adapter accepts Chinese and English with an exact reference transcript.
Its English quality has not passed the Qiqi evaluation.
Neither engine claims native streaming playback.

## Optional final ASR refinement

The supervisor accepts this final-model option:

```text
--final-model ~/models/sherpa-onnx/clone-test-downloads/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17
```

SenseVoice decodes accepted PCM spans after each final Zipformer result.
Model initialization and decoding run outside the online recognition loop.
Refinement updates do not start another translation or repeat speech.
The bundle comes from the [official sherpa-onnx ASR release](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2).
The recorded hashes are:

```text
model.int8.onnx
c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51
received archive
7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e
```

The archive hash identifies the received file; it is not an independently published upstream digest.
A fixed six-second Mandarin fixture decoded in 0.48 seconds with normalized character error rate zero.
Live endpoints can still split words.
A two-minute speech-only replay kept at least 2.84 GiB of available RAM.
Fast speech still caused reported queue skips.

These results do not prove translation accuracy or long-run coverage.
The [reliability report](../reports/reliability-20261006/RESULTS.md) gives the tested excerpts, conditions, and failures.

## Regression procedure

Run the Python regressions:

```bash
python3 -m unittest discover -s tools/tests -v
```

Native tests cover continuous PCM across ASR segments and resets after frame or clock gaps.
DSP tests cover VAD startup, a 200 ms pause, and sustained silence.
The Golden and captured-tensor ONNX procedures in [AGENTS.md](../AGENTS.md) are necessary for RVC neural comparisons.
