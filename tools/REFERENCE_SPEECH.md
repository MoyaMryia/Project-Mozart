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
It rejects occupied ports and waits for speech engine readiness before capture.
It drains the final caption and stops its children on exit.
It does not start the frontend or optional RVC monitor.

In a different terminal, start the frontend:

```bash
npm --prefix frontend run dev
```

## Translation memory settings

The supervisor limits the prompt-state cache to 128 MiB.
The logical and physical batch sizes are 256 and 128.
The recorded translator runtime had an 8 GiB default cache limit.
These limits prevent that default from determining the deployment memory budget.

The options are `--translation-cache-mib`, `--translation-batch-size`, and `--translation-ubatch-size`.
The `--llama-model` option selects a different model.
Candidate models must pass full quality and memory tests before deployment.

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
Four final utterances can wait behind the active request.
When this queue is full, the bridge keeps the source caption and reports a skipped translation.
These skipped utterances count as missing translated coverage.
The subtitle revision increases for each update; fixed revision numbers do not identify processing stages.

### Numeric checks

Explicit digits, spoken four-digit years, and large quantities receive numeric checks.
A failed check permits one fresh translation retry.
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

SenseVoice uses a second decode for explicit amounts or years of at least 100.
Both decodes use the same PCM and one loaded model.
The second decode uses the literal-number mode.
Conflicting values or ambiguous repeated units produce `asr_numeric_audit` and `speech_skipped_reason=asr_numeric_uncertainty`.
The bridge keeps both readings and sends no translation or speech request.
Agreement between readings does not prove transcript accuracy.

Strict 可能/不可能 conflicts produce `asr_polarity_audit` and `speech_skipped_reason=asr_polarity_uncertainty`.
The bridge keeps the original streaming and final readings.
Their endpoint spans can differ.
Other negation, names, pronouns, and domain terms are outside this check.

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
The service stores up to 128 logical results.
A 64-job limit bounds unfinished work.

## Admission and deadlines

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

Engine load has a 90-second timeout.
Live piece synthesis has a 20-second timeout.
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
A restart marks interrupted jobs as failed and keeps completed downloads.
A new profile is necessary for a different engine.

The optional `--engine zipvoice --vocoder PATH` adapter accepts Chinese and English with an exact reference transcript.
Its English quality has not passed the Qiqi evaluation.
Neither engine claims native streaming playback.

## Optional final ASR refinement

The supervisor accepts this final-model option:

```text
--final-model ~/models/sherpa-onnx/clone-test-downloads/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17
```

SenseVoice decodes the PCM span of each final Zipformer utterance.
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
