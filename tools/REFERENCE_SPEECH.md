# Translated reference speech on Jetson

The zero-shot feature synthesizes **translated text in a reference voice** using
[Kyutai PocketTTS](https://github.com/kyutai-labs/pocket-tts) through the
[sherpa-onnx reference-audio API](https://k2-fsa.github.io/sherpa/onnx/tts/pocket.html).
The pinned English bundle is `sherpa-onnx-pocket-tts-int8-2026-01-26`; the tested
runtime is sherpa-onnx 1.13.6. Existing RVC modes and their audio contract are
separate. Legacy `rt_zero_shot` / `file_zero_shot` speech-conversion modes still
return 501; reference TTS uses the endpoints below.

## Start the stack

Build the project and frontend first. On the configured Jetson:

```bash
.venv/bin/python tools/run_translated_speech.py \
  --backend-config state/translated-speech.yaml \
  --reference reference.wav --reference-name "My voice" \
  --input test-audio.mp4 --seconds 60
```

Omit `--input` to capture the USB microphone. `--start 120 --seconds 600`
replays a ten-minute excerpt at normal microphone pace. Capture audio never
contains synthesized output. `--no-playback` records results without opening
ALSA. Named devices default to `plughw:CARD=Device,DEV=0` and are configurable.
The translated-speech configuration disables RVC capabilities and avoids loading
unused HuBERT/RMVPE/Generator engines. Use the Qiqi backend configuration when
optional simultaneous RVC monitoring is needed.

For a demo without physical audio, use `--input test-audio.mp4 --start 720
--seconds 120 --playback-device null --keep-open`. The file is replayed at
microphone pace and null output waits for each generated clip's actual duration.
After the input and queued speech finish, the API and voice previews remain
available until the supervisor is stopped. Capture and ASR have ended at that
point; the existing child-process and 768 MiB memory guards remain active.

The supervisor owns the native API, ASR, translator and speech service, refuses
occupied ports, checks memory, drains the final caption and stops its children.
It does not start the optional RVC monitor or frontend server.

Translator prompt-state caching is limited to 128 MiB, with logical/physical
batch sizes 256/128. The installed runtime's default cache ceiling is 8 GiB.
`--translation-cache-mib`, `--translation-batch-size` and
`--translation-ubatch-size` expose these bounds; `--llama-model` selects an
explicit alternative model. Candidate models need full-stack memory and quality
validation before adoption.

In another terminal, `cd frontend && npm run dev -- --host 0.0.0.0` exposes the
control UI. The Reference Speech panel uploads 3–15-second WAVs (8–48 kHz,
mono/stereo, at most 5 MiB), generates downloadable previews, selects the live
voice and stops queued speech. A successful preview means synthesis completed;
voice identity and pronunciation still need listening. Profiles do not claim
human identity approval automatically.

For independent service management:

```bash
.venv/bin/python tools/speech_service.py \
  --model ~/models/sherpa-onnx/clone-test-downloads/sherpa-onnx-pocket-tts-int8-2026-01-26 \
  --playback-device plughw:CARD=Device,DEV=0
```

The native API proxies `/api/voices` and `/api/speech/*` to loopback port 18081
(`MOZART_SPEECH_PORT` overrides it). A missing service returns HTTP 503 while RVC
and captions remain available. For diagnostic replays, `--archive-utterances` saves processed 16 kHz PCM16
source snippets with hashes and timestamps. This is off by default; the original
input remains the source reference.

The bridge must run with `--speak`; admission
uses the selected speech session and never reads source-language text as a
fallback. Explicit digits, spoken four-digit years and large quantities receive numeral
checks with one fresh translation retry. Repeated quantities, invented large
amounts/years, untranslated Chinese and token-limit truncation are rejected.
Calendar month names are accepted as equivalent only when they cannot mask
another missing quantity. Original Chinese and attempted English are retained
in each caption's translation audit. Explicit standalone 万/亿 quantities are
normalized to equivalent numerals/ranges before translation. Exact Chinese
composites such as 七万五千 are normalized atomically to 75,000 rather than
licensed as separate 70,000 and 5,000 amounts. Repeated explicit quantities must
retain their occurrence counts. Vague, malformed, mixed and colloquial shorthand
remain unnormalized. A failed check publishes the source caption
and an error without speaking the suspect translation. These checks do not prove
semantic accuracy; preserve source captions for comparison.

SenseVoice utterances with explicit amounts or years of at least 100 receive a
second decode of the same PCM using its literal-number mode. Both streams reuse
one loaded model. Conflicting values or ambiguous repeated numeral units publish
`asr_numeric_audit` and `speech_skipped_reason=asr_numeric_uncertainty`; the bridge
retains the source and both readings and makes no translation/speech request.
It never chooses a replacement amount. Agreement is not a verified transcript.
Other recognition errors and small counts remain outside this narrow check.
The bridge also withholds speech when both recognizers contain strict 可能 /
不可能 cues but disagree on their polarity/counts. The original streaming and
final readings remain in `asr_polarity_audit`, with
`speech_skipped_reason=asr_polarity_uncertainty`. Their endpoint spans can differ;
the warning is uncertainty, not a replacement transcript. Other negation,
pronouns, names and domain terms are not validated by this check.

The bridge also reports `asr_boundary_audit` and
`speech_skipped_reason=asr_boundary_uncertainty` for two reproduced unresolved
boundary forms: a clause ending after 不管自己…在, and the ambiguous repeated
mother-doubt subject. It preserves the original caption and makes no translation
or speech request. These warnings are unresolved context, not corrected readings
or a general grammar detector. Blocked captions remain unspoken source coverage.
Blanket endpoint merging is not enabled: fixed-audio controls recovered some
words but fused amounts and added excessive waiting time.

Narrow translation constraints preserve explicit 他不管 in third person and
reject adding herself to the measured mother-doubt construction without 自己.
The attempt audit records `explicit_role_issues` and `source_role_constraints`.
These checks are not a complete role/meaning evaluator. The local source-review
benchmark under `rvc-golden/sentence-context-20261007` retains continuous input,
automatic drafts and a review/export page; draft text cannot be scored as a
human-reviewed reference.

## API

| Endpoint | Contract |
| --- | --- |
| `GET /api/speech/status` | Engine, proven output languages, session, voices, jobs and queue limit |
| `POST /api/voices` | JSON `{name,audio_base64,language,transcript}`; WAV bytes encoded as base64 |
| `GET /api/voices` | Persistent immutable reference profiles with SHA-256 revisions |
| `DELETE /api/voices/{id}` | Reject deletion while an unfinished job uses the reference |
| `POST /api/speech/jobs` | `{text,language,voice_id,playback?,utterance_id?,live?}` → queued job |
| `GET /api/speech/jobs/{id}` | Observable queued/processing/ready/playing/completed/failed/cancelled/expired state |
| `GET /api/speech/jobs/{id}/result` | Completed WAV only |
| `DELETE /api/speech/jobs/{id}` | Cancel; terminate active synthesis or playback |
| `POST /api/speech/session` | `{enabled,voice_id,language,playback}` selects translated speech |
| `POST /api/speech/events` | Stable final translation `{text,utterance_id}` from the bridge |
| `POST /api/speech/stop` | Disable live admission and cancel all unfinished jobs |

Live playback admission uses an estimated 24-second audio budget, including
remaining active playback, instead of counting ready/playing jobs against four
synthesis slots. A 64-job safety cap bounds bookkeeping; download-only admission
allows four pending/active synthesis jobs. There are 128 retained logical results.
When the audio budget is temporarily full, up to four text jobs wait in FIFO
order without reserving audio or starting synthesis. Their original 30-second
deadline remains in effect; a full text queue or oversized single job returns
429 and captions expose the error. `admission_pending`, `was_deferred` and
`admission_wait_seconds` show this wait. Live playback jobs that have not
started playing expire after 30 seconds; download-only jobs use first-piece
readiness for that deadline. Started speech finishes its ordered pieces.
Projected first-play admission includes preceding deferred text, so a sentence
predicted to miss its deadline is rejected before synthesis. Such rejection is
unspoken coverage, not successful throughput. The engine is preloaded and its
readiness confirmed before microphone/file capture starts.
Duplicate utterance IDs return the previous logical job. Engine load has a
90-second timeout; live pieces have a 20-second synthesis timeout. Cancellation
terminates active synthesis/playback and removes the remaining queued pieces.

Live or played text is split at sentence/clause/word boundaries, targeting 12
English length units (ordinary words count as one, three-digit numerals as two,
and four-or-more-digit numerals as four; up to 15 to avoid tiny tails or finish
a nearby ordinary-word sentence), or 24
Chinese characters per piece. A leading numeric clause may split after three
written words because the numeral expands when spoken. These units guide
boundary selection; admission estimates and audio buffer limits stay unchanged.
Adjacent identical clauses of at least three words are generated separately
when they share a piece; every intentional repetition is retained. This avoids
the extra phrase observed in the fixed-reference repeated-clause control.
No translated text is removed. Each piece is generated as a complete WAV and
can play while subsequent pieces generate; this is chunked playback, not engine
token streaming. A completed logical job still has one ordered downloadable WAV.
Generated duration per played/live piece is limited to 12 seconds; non-live
download-only generation retains the 45-second ceiling. Excessive/empty/nonfinite or unsafe-amplitude audio
is rejected. Ordinary peaks receive a measured scalar gain before PCM16 encoding
to prevent clipping; the raw peak and output gain are included in each job. Failed
results are never published as successful previews. A synthesis worker overlaps
with one ALSA output owner. Buffered audio, including the active player's
remainder, is bounded to 12 seconds. Generation pauses before computing the next
piece when the estimated buffer is full; an unexpectedly long finished piece
waits until its actual duration fits instead of being discarded. ALSA null output
is paced to generated audio duration for meaningful queue tests.

Pocket synthesis input receives terminal punctuation and collapsed ellipses;
the original text and all words/numbers stay unchanged. Each piece records
`synthesis_text` so this adjustment is auditable. For live or multi-piece jobs,
the complete `GenerationConfig.extra` map sets both seed 42 and `max_frames`.
The native map getter returns a copy, so in-place dictionary updates lose the
frame cap. The cap bounds the latent loop before decoding callbacks run;
outputs reaching the cap are rejected as having an unverified ending.

References persist under `~/.local/share/mozart/speech/voices`; completed jobs
persist under `results`. Restart marks interrupted jobs failed and preserves
completed downloads. Changing the engine requires creating an engine-specific
profile. An optional `--engine zipvoice --vocoder PATH` adapter supports Chinese
and English with an exact reference transcript; its English quality has not
passed the Qiqi evaluation. Neither engine advertises streaming playback.

## Checks

```bash
python3 -m unittest discover -s tools/tests -v
```

Native streaming tests cover preservation of continuous PCM across ASR segments
and reset on frame gaps/backward/large-forward clocks. DSP tests cover energy
VAD startup and 200 ms pause versus sustained silence. RVC Golden artifacts and
captured-tensor ONNX parity must still pass before neural quality comparisons.

## Optional final ASR refinement

`--final-model ~/models/sherpa-onnx/clone-test-downloads/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17`
adds SenseVoice to refine the PCM span of each final Zipformer utterance. Obtain
that bundle from the [official sherpa-onnx ASR release](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2).
The tested `model.int8.onnx` SHA-256 is
`c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51`.
The download archive SHA-256 was
`7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e`;
this records the received archive, not a separately published upstream digest.

This option remains experimental: the fixed six-second Mandarin fixture decoded
in 0.48 seconds with normalized CER 0, but live endpoint spans can still split
words. A two-minute paced speech-only run retained at least 2.84 GiB of available
RAM; fast speech still caused explicitly reported queue skips. Neither the
numeric guard nor this ASR refinement proves translation accuracy. See the
dated results report for the tested excerpts, playback conditions and failures.

The supervisor preloads the speech worker and waits for runtime readiness before
starting capture, keeping cold initialization outside real sentence deadlines.
Sentence splitting preserves existing clause boundaries before extending a short
sentence tail; numeric ranges count both endpoints, and suitable because/but
boundaries keep inventory-value phrases together. These boundaries preserve all
original words and quantities. Candidate translator evaluations and their current
validation status are recorded in reports/reliability-20261006/RESULTS.md.
