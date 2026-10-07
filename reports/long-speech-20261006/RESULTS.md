# Mozart 30-minute translated reference speech test — October 6, 2026

The run completed with supervisor exit 0 and all 90,000 native input frames sent without overruns. It retained all 244 completed WAVs, totaling **1,358.64 seconds (22 minutes 38.64 seconds)**. Runtime stability passed this workload, but **only 244 of 407 caption finals (59.95%) completed speech playback**. The system remains a development prototype for continuous translation.

## Workload and boundaries

- Start: 07:15:43 Asia/Singapore, October 6, 2026. Total harness wall time: 1,845.96 seconds, including startup and drain.
- Source: the supplied `test-audio.mp4`, seconds 720–2520 (minutes 12–42). Input SHA-256: `2b80f2d78c59c530b53cb96e8a8aef9852f169231f312a67ce352a19db61b389`.
- Path: native paced PCM microphone replay → preprocessor/UDP → online Zipformer plus SenseVoice final refinement → local Qwen 0.8B translation → PocketTTS with Qiqi reference → paced ALSA null output. RVC was disabled; its neural engines were not constructed.
- No application code was changed during this baseline. No new model was downloaded. The harness archived each completed result before the service could evict its 128-result history.
- Output was **paced null**, not a physical speaker soak or acoustic latency measurement. Human pronunciation, semantic translation and voice identity were not approved. The previous short USB playback check is separate evidence.

## What happened to every final caption

| Outcome | Count |
| --- | ---: |
| Completed speech playback | 244 |
| Queue admission rejected (429) | 105 |
| Unsupported text/language admission (422) | 19 |
| Bad request admission (400) | 3 |
| Translation numeral check withheld speech | 6 |
| Synthesized speech skipped by playback queue | 24 |
| Generated duration limit rejected speech | 1 |
| Live deadline expired | 5 |
| **Total** | **407** |

All 274 admitted jobs reached a terminal state: 244 completed, 25 failed and five expired. There were no missing completed WAVs. All caption finals remain recorded even when speech was skipped. The counts account for all 407 finals exactly. Completion percentage counts utterances; it is not a measure of words translated correctly or content coverage.

The queue never exceeded four unfinished jobs. Waiting playback reached 16.24 seconds: the current policy intentionally allows one sentence to exceed the nominal 12-second waiting limit. This is a policy exception, not an observed universal 12-second bound. The 24 playback failures explicitly report that speech was skipped.

The duration failure occurred on an incomplete sentence ending `One is kidnapping someone, the other is...`; generation exceeded its 20-second allowance and was rejected after about 30 seconds wall time. Its worker interruption/reload caused additional pressure. No unsafe result was published as completed. Four expired jobs had generated audio; one expired before generation. One expired job reached terminal state at 43.81 seconds age: the 30-second freshness check prevents stale playback, but does not guarantee termination precisely at 30 seconds.

## Resource and timing measurements

| Measurement | Median | p95 | Maximum |
| --- | ---: | ---: | ---: |
| Translation | 356 ms | 1,259 ms | 5,898 ms |
| Synthesis RTF | 0.750 | 0.833 | 1.002 |
| Speech enqueue → complete WAV ready | 6.88 s | 12.60 s | 29.38 s |
| WAV ready → playback start | 4.69 s | 14.08 s | 17.64 s |
| Speech enqueue → playback start | 10.73 s | 25.90 s | 29.84 s |

Synthesis RTF is generation time divided by generated audio duration. Queue/playback timing covers only completed jobs and starts after final translation admission. It excludes capture, endpoint waiting, ASR refinement and translation; it is not microphone-to-speaker latency. Skipped and expired jobs are not hidden in the completion counts above.

Minimum system available RAM was **1.69 GiB**. Swap was already in use: 844,390,400 bytes at baseline/median and 844,652,544 bytes at maximum, an increase of 0.25 MiB. The run did not exhaust RAM or hit the supervisor's low-memory guard. Available memory declined during the run; per-process RSS was not recorded, so these measurements do not establish whether allocator growth or another system workload caused that decline. This is not evidence of swap-free operation or a proven absence of leaks.

The preprocessor sent **90,000 frames in 1,800 seconds**, with **zero overruns**, and ended cleanly. Runtime listener and process cleanup were checked after supervisor shutdown; no workload ports remained listening. The harness's successful exit includes supervisor cleanup. Source/control changes from previous work and the Jetson's existing commits were preserved.

## Audio and intelligibility

Every completed PCM WAV was scored for format/health: **244/244 finite**, maximum absolute peak **0.980011**, and maximum clipping fraction **0%**. The concatenated file contains exactly their audio in submission order, with no original gaps. It omits all rejected/skipped/expired sentences, so it does not reconstruct the full 30-minute source or actual playback timeline. The source and translated text of each retained sentence are in [the segment manifest](/home/moyamryia/Projects/Project-Mozart/rvc-golden/long-speech-20261006/combined-segments.json).

Cached official Whisper-small scored ten deterministic samples after timed services exited: eight evenly spaced completed jobs plus shortest and longest. Weighted word error against the submitted TTS text was **9.40%**, median **11.86%**. All completed sentences were within the scorer's 29-second window; no long sample was silently truncated. These are automatic transcription comparisons, not independently verified TTS ground truth or human identity ratings. Names, spelling variants and numeric formats can contribute errors.

Concrete sampled findings:

- The longest completed sentence (19.52 seconds) repeated `You told me how much you owed?` beyond the submitted text.
- One 13.28-second sample added another `it's not a way to do it` repetition.
- `All money has been recovered` transcribed as `All my has been recovered`.
- `70,000 yuan` transcribed as `70,000 won`; human listening is needed to attribute this to pronunciation versus ASR.
- Three of ten samples matched the requested text under the scorer's word normalization, including a 12.32-second sentence. Correctly spoken text does not prove its preceding translation was correct.

The recognition/translation path still contains clear semantic problems. A recorded source caption beginning `接下来的行为就不是你自己能决定的了` continued with mangled words and produced `my shadow puppeteer attempted to play a script identical to an iPad, but it was lost to the rain`. The TTS intelligibility check mostly matched that English text, demonstrating why TTS success cannot substitute for ASR/translation validation. Six translations were withheld by numeral checks; those checks can also reject valid written month names and do not establish semantic accuracy.

## Assessment and next changes

The longer test supports running the speech-only stack on this Jetson within the observed resource budget. It does **not** support dependable live interpretation: about 40% of final captions did not complete speech, accepted speech began after a median 10.73 seconds of post-translation queueing/synthesis, and sampled outputs still repeated words.

Priorities are:

1. Reduce speech backlog with duration-aware admission, shorter well-formed sentences and genuine streaming output. Require a stated completion and latency target rather than treating explicit skips as product success.
2. Fix ASR final-span alignment and evaluate colloquial Mandarin translation against annotated reference text. Correct numeral handling for dates and named quantities. Keep source captions visible.
3. Address repetition and incomplete/very short TTS prompts; compare engines and reference choices using the same text/audio fixtures. Verify Qiqi's cross-language identity by listening.
4. Record per-process RSS and test physical microphone/output behavior in the next sustained run. Repeat the same load after changes to compare completion rate, latency and resource use fairly.

[Smaller MP3 listening preview](/home/moyamryia/Projects/Project-Mozart/rvc-golden/long-speech-20261006/qiqi-translated-30min-preview.mp3) · [Combined 22-minute-39-second WAV](/home/moyamryia/Projects/Project-Mozart/rvc-golden/long-speech-20261006/qiqi-translated-30min-combined.wav) · [Raw summary](/home/moyamryia/Projects/Project-Mozart/rvc-golden/long-speech-20261006/summary.json) · [Automatic intelligibility samples](/home/moyamryia/Projects/Project-Mozart/rvc-golden/long-speech-20261006/intelligibility-samples.json) · [Audio health](/home/moyamryia/Projects/Project-Mozart/rvc-golden/long-speech-20261006/audio-health.json) · [Run protocol](/home/moyamryia/Projects/Project-Mozart/rvc-golden/long-speech-20261006/protocol.json)

The download archive and all 244 archived WAVs plus the combined file were checked by SHA-256 after collection. The smaller MP3 is a lossy listening derivative; objective scoring used the original PCM WAVs. The scheduled follow-up was paused after reporting completion. This report records a completed baseline, not a claim that the listed quality limitations were fixed.
