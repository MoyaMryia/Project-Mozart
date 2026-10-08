> 历史记录：本文保留当时的测量、日志与审查原文。当前部署和接口以[维护文档](../../README.md)为准。

# Smaller speech pieces — 2026-10-06

**Status: completed and fully collected/verified. Smaller-piece runtime validation passed: 365/379 valid jobs completed with no failures, expiry or overruns. Both repaired phrases passed targeted transcription. Remaining translation/term-pronunciation issues and a possible extra romanized-term repetition are documented; this bounded campaign has ended without another automatic rerun.**

The previous 132.56-second output was assembled from 22 separate TTS jobs, rather than generated in one call. However, a long individual sentence still blocked playback until its entire WAV was ready. Smaller generation pieces address that delay; they do not eliminate playback time or guarantee every sentence fits a real-time budget.

## Changes deployed

- Live/played English text is divided at sentence, clause and word boundaries, targeting 12 length units and allowing up to 15 for a short tail. Ordinary words count as one; v4 adds numeral expansion weights as described below. Prepositions and determiners remain with their following phrase at hard cuts. Text, numbers and ordering are preserved. Chinese uses bounded character pieces; Chinese synthesis was not exercised in these English-output replays.
- Each finished piece can play while later pieces generate. A logical job retains one ordered downloadable WAV. This is piece-based playback, not model token streaming. Cancellation stops both stages and removes remaining pieces.
- Live admission uses an estimated 24-second audio budget including active playback, with a separate 64-job bookkeeping cap. Download-only synthesis retains four pending/active slots. Buffered audio, including the active remainder, is bounded to 12 seconds; synthesis waits before generating more. Unexpectedly long pieces wait for actual buffer capacity instead of being discarded.
- Speech must begin within 30 seconds; already-started speech can finish its remaining pieces. Live piece synthesis has a 20-second timeout and a 12-second duration ceiling. The initially added Pocket latent-frame cap was ineffective because the native options getter returns a copy. The v3 correction assigns the full options dictionary through its setter; reaching the cap rejects the result rather than accepting an unverified truncated ending.
- The UI shows the duration budget and playback progress. Frontend type-check/build passed. All 26 speech, cancellation, restart, translation and boundary regression checks passed locally and on Jetson.

## Same two-minute input

Source: seconds 720–840 of `test-audio.mp4`, replayed at microphone pace. Same Qiqi reference, PocketTTS five steps/seed 42/two CPU threads, SenseVoice final refinement and Qwen CUDA translation. RVC disabled. ALSA null output was paced to actual audio duration. Source ASR and translated texts match the original controlled baseline exactly.

| Result | Original paced baseline | First piece-based attempt | Corrected attempt |
| --- | ---: | ---: | ---: |
| ASR finals / valid translations | 24 / 22 | 24 / 22 | 24 / 22 |
| Completed logical jobs | 14 | 19 | **20** |
| Admission budget/queue skips | 6 | 2 | **2** |
| Admitted generation/playback failures | 1 | 1 | **0** |
| Expired jobs | 1 | 0 | **0** |
| Native frames / overruns | 6,000 / 0 | 6,000 / 0 | **6,000 / 0** |
| Completed translated words / valid offered words | 284 / 367 (77.4%) | — | **322 / 367 (87.7%)** |

Both piece-based attempts retained the same two invalid translations: one overlength and one containing Chinese. The first attempt failed on a fragment ending with “to”; generation repeated past its duration guard after earlier pieces had played. That attempt and its partial audio were retained. The boundary correction was then deployed and tested in a fresh attempt.

Corrected `short-v2` generated **40 pieces**: median duration **3.00 seconds**, 95th percentile **6.08 seconds**, maximum **6.56 seconds**. Median piece synthesis was **2.16 seconds**, maximum **4.78 seconds**. Six completed multi-piece jobs began playing before generation finished. Maximum actual buffered playback was **10.14 seconds**. Minimum available RAM was **3.44 GiB**; swap-in was zero. Recorded aggregate median CPU use was approximately 44% across startup, input and drain, and should not be compared directly to an active-generation-only population.

On 13 jobs completed with identical translations in both baseline and corrected runs, median submission-to-first-playback was approximately unchanged: **15.99 → 15.96 seconds**. Median submission-to-first-ready improved **9.94 → 8.79 seconds**. The corrected run's overall median first-playback delay was 16.41 seconds, with a maximum of 24.61 seconds. Increased job completion is the main demonstrated improvement; a broad latency reduction is not established.

Job completion does not equal semantic or word coverage. The saved [word coverage measurements](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/word-coverage.json) count words in completed translated text against all valid offered translations. They do not prove every word was pronounced correctly. Two valid captions still exceeded the estimated live budget and remain a performance limitation to investigate.

## Audio verification

All 20 completed WAVs were finite, with zero clipping and maximum peak 0.8964. Across 20 joins inside completed multi-piece jobs, the maximum adjacent-sample step was 0.00742. This is an objective continuity statistic, not proof of inaudible boundaries.

Cached Whisper-small scored nine deterministic completed samples: weighted WER **11.35%**, median **10.71%**. Samples include recognizer/spelling differences and a possible repeated “of gold coins to her mother” phrase in the first multi-piece sentence. That case remains flagged for inspection; neither low aggregate WER nor text-preserving segmentation proves correct audio or human voice similarity. The older long-run 9.4% score used different samples and is not a paired quality baseline.

Both short attempts were collected locally. Verified 39 logical-result WAV hashes, 75 piece WAV hashes, and both combined WAV hashes. Archive SHA-256: `e25a956a4fa3a2195b169e11b13cf6374de81a65416d80f2e32a21056cdfcb76`.

The corrected combined WAV is **124.48 seconds (2:04.48)**. It joins the 20 completed sentences and omits original pauses and skipped captions; the player generated and played individual short pieces during the run.

- [Corrected combined WAV](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/short-v2/qiqi-translated-combined.wav).
- [First multi-piece sentence](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/short-v2/archive/178d88a215b240d6ac5abed6a0aac3f2.wav).
- [Short-run metrics](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/short-v2/summary.json), [sampled transcriptions](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/short-v2/intelligibility-samples.json), [collection and matched comparison](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/short-collection-receipt.json).

## Finished long-v2 validation

`long-v2` replayed source seconds 720–2520. The supervisor exited successfully after 1,846.83 seconds, all owned services stopped, and the campaign lock was released. Native input sent **90,000 frames with zero overruns**. All **407 recognized and translated captions match the original long baseline exactly**, so changed recognition or translation cannot explain the differences below.

| Result | Original 30-minute run | long-v2 |
| --- | ---: | ---: |
| Valid offered translations | 379 | 379 |
| Completed jobs | 244 (64.4%) | **353 (93.1%)** |
| Completed text words / offered words | 3,938 / 5,258 (74.9%) | **4,607 / 5,258 (87.6%)** |
| Admission skips | 105 | **23** |
| Admitted failures / expiry | 25 / 5 | **3 / 0** |
| Generated-audio playback-capacity failures | 24 | **0** |

Both runs rejected three overlength translations, 19 containing unsupported Chinese, and six translations failing numeral preservation. The new admission skips account for **586 offered English words**. Completed-text coverage does not prove every word was spoken correctly, and partial failed sentences are excluded from complete-job coverage.

The run generated **589 pieces**, including four retained pieces from two partially played failed jobs. Piece duration: median **2.64 seconds**, p95 **5.04**, maximum **7.84**. Piece synthesis: median **2.00 seconds**, maximum **5.78**. Seventy-six multi-piece jobs started playing before their parent generation finished. Actual buffered audio stayed below **10.39 seconds**. No successfully synthesized pieces were discarded before playback; 14.8 seconds of partial failed speech played but is excluded from the combined completed-job WAV. Failed-call compute was not separately instrumented in this attempt, so it must not be equated to failed-job wall time.

On **227 matched completed jobs**, median submission-to-first-ready changed **6.27 → 5.45 seconds**; its p95 changed **12.22 → 13.97**. Median submission-to-first-playback changed **10.61 → 11.63 seconds**, while p95 improved **25.63 → 20.71**. Coverage and the playback-delay tail improved; general latency reduction is not demonstrated.

Aggregate CPU: median **53.0%**, p95 **62.7%**, maximum **76.4%** of all six cores. The clone worker's median use was approximately **2.26 core equivalents**, with median RSS **0.587 GiB**. Available RAM fell to **1.841 GiB**; recorded swap ranged **787–944 MiB**, with **8,459 swap-in pages (about 33 MiB)**. This is not sustained CPU saturation, but memory pressure and growth still limit claims about longer operation. Translator RSS peaked around **3.15 GiB**; RSS accounting on Jetson is not a precise split between CPU and shared GPU memory.

All 353 completed WAVs were finite, with zero clipping. Nine deterministic Whisper samples scored **14.77% weighted WER**; this population differs from the older long-run samples. The identical first sentence scored 0 edits in the baseline and six inserted words in long-v2, matching the short-v2 repeated phrase warning. Another long sample has omissions/repetition. The maximum internal boundary step was **0.2313**, so inaudible joins are not established. These quality issues require validation of the next correction, not waveform trimming to improve scores.

The retained combined output is **1,613.76 seconds (26:53.76)**, assembled from 353 completed logical jobs. It joins completed sentences and omits source pauses, admission skips, rejected translations and partially failed sentences. It is not one TTS call.

All long-v2 artifacts are now local. Verified **353 logical WAV hashes, 589 piece hashes and the combined hash**, plus exact ordered piece-to-parent PCM assembly and text preservation. Transfer archive SHA-256: `945384debd4456e9627fc4b6821c124b95c6d0202c052cc22a59b0c88cf3e73f`.

- [Retained long-v2 combined WAV](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v2/qiqi-translated-combined.wav).
- [Long-run matched comparison](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v2/comparison.json), [audio verification receipt](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v2/collection-receipt.json).

## Termination correction and next validation

The three failures were an unfinished fragment ending in “a loan”, a piece ending in an ellipsis, and the single word “yes”. The installed sherpa-onnx 1.13.6 binding demonstrated that `config.extra['max_frames'] = ...` mutated a temporary copy. Assigning the complete dictionary preserves the frame cap. The upstream [Pocket implementation](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.6/sherpa-onnx/csrc/offline-tts-pocket-impl.h) confirms that the latent loop precedes audio callbacks and that the limit applies per internal sentence.

Fixed-reference controls retained seed 42, five steps, two threads and the original limits. With the cap applied, the unpunctuated “loan” and “yes” inputs still reached their caps; the ellipsis exceeded the total duration guard. Adding terminal punctuation or replacing the ellipsis with a period produced **2.08, 0.56 and 4.08 seconds**, respectively, without saturation. The worker now changes only punctuation for its synthesis input and saves that input in piece metadata; original caption text and every word/number remain intact. Cached Whisper found no repeated phrases in those three repaired controls, but confused “a loan” with “alone”. This is automatic transcription, not human voice-similarity approval.

All **32 regression tests passed on both hosts**, including a copy-on-read native-options regression; the actual Jetson binding retains `max_frames=50`. Buffers, playback tempo and source text have not been enlarged, accelerated or discarded to manufacture success.

## Passed short-v3 and current long-v3

The fresh two-minute validation completed **21/22 valid jobs**, with **zero failures, expiry or capture overruns**; the source ASR and translations still match the original controlled baseline. One budget skip accounts for **39/367 offered English words**, giving completed-text word coverage **328/367 (89.4%)** versus 77.4% originally. Completion fraction 95.5% should not be mistaken for word coverage.

Its **41 pieces** had median **2.96 seconds**, p95 **5.12**, maximum **5.76**; median synthesis **2.20 seconds**, maximum **4.25**. Actual buffered playback peaked at **9.54 seconds**. Minimum available RAM was **3.548 GiB**, with 11 swap-in pages, median CPU **48.6%** and maximum **70.9%** across the measured run. All 21 WAVs were finite with zero clipping; the maximum internal boundary step was **0.00540**.

For 13 completed jobs matched to the original short baseline, median first-ready delay improved **9.94 → 6.52 seconds** and median first-playback **15.99 → 11.20 seconds**. The ready-delay maximum increased **12.39 → 14.90 seconds**; do not infer uniform improvement. The full short-v3 population's median first-play delay was **13.46 seconds**, maximum **20.67**.

Nine deterministic cached Whisper samples scored **4.55% weighted WER**, median **2.70%**. The first 41-word multi-piece sentence now has **zero word edits**, versus six inserted words in short-v2/long-v2. Its duration is **15.52 seconds**, comprising five pieces; the original unsplit rendition was 12.32 seconds. Automatic transcription is evidence against the known repetition, not human approval of voice similarity.

Short-v3 was collected locally: verified **21 parent WAVs, 41 pieces and the combined WAV**, including exact ordered PCM assembly and preservation of all original text and synthesis words. Transfer archive SHA-256: `b3b943fe99bfc4b6aef7bcdb61e38309d458b0e9d860b1e84172c3b0c9e59bc6`.

The corrected combined WAV is **121.44 seconds (2:01.44)**, joining 21 completed logical jobs and omitting original pauses, skipped captions and rejected translations. Individual pieces were played during the test; this is not a single 121-second synthesis call.

- [Corrected short-v3 combined WAV](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/short-v3/qiqi-translated-combined.wav).
- [Representative five-piece sentence](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/short-v3/archive/5714107587c4451e934fbaf8fae94131.wav).
- [Short-v3 metrics](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/short-v3/summary.json), [matched comparison](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/short-v3/comparison.json), [sampled transcriptions](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/short-v3/intelligibility-samples.json).

Only after short-v3 and offline scoring exited successfully was **long-v3** launched once, replaying the same source seconds 720–2520. The bounded watchdog remains 2,160 seconds. The active attempt is determined from campaign state, not from a stale scheduled prompt.
Startup was verified at 50.84 seconds: three completed jobs were already archived, the API had no error, and available RAM was approximately 3.68 GiB. Harness PID 849488 and supervisor 849489 own this attempt.

The existing follow-up automation was updated to check every ten minutes. It stays quiet while healthy, collects and scores only after workload exit, investigates concrete failures or quality regressions, verifies fixes with new short attempts, and runs subsequent long attempts sequentially. It must preserve unrelated changes and remote commits, avoid duplicated active tests, and report remaining budget skips, repetition and latency honestly. After three additional unresolved repair cycles it reports the remaining limitation and required decision instead of endlessly replaying inference.

Current [campaign state](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/campaign-state.json) and the remote run files are authoritative for progress. Physical speaker behavior and human voice similarity remain unverified. No GPU runtime migration or RVC neural-pipeline change was made in this work.

## Finished long-v3 and numeric-boundary repair

Long-v3 exited cleanly after **1,845.53 seconds**. All owned services stopped before quality inference. Its 407 source recognition and translation outputs exactly match both prior long runs. It sent **90,000 native frames with zero overruns**, completed **364/379 valid jobs (96.0%)**, and had **zero failures or expiry**. Fifteen admission skips retained their captions and accounted for **456/5,258 offered English words**; completed-text coverage was **4,802/5,258 (91.3%)**. The same three overlength, 19 unsupported-language and six numeral-preservation translation rejections remain.

The run generated **608 pieces**: median **2.64 seconds**, p95 **4.64**, maximum **10.64**. Median piece synthesis was **1.95 seconds**, maximum **7.73**. Seventy-eight jobs started playback before complete parent generation. Actual buffered playback peaked at **10.84 seconds**; no successfully synthesized audio was discarded or left unplayed. Combined completed output is **1,657.60 seconds (27:37.60)**, joining completed sentences and omitting original pauses, skips and rejected translations.

On 229 matched completed translations, median first-ready delay changed **6.46 → 4.96 seconds**. Median first-play delay was approximately unchanged **10.61 → 10.95**, while p95 improved **25.63 → 18.37** and maximum **29.84 → 21.13**. Aggregate CPU median/p95/maximum were **50.6% / 60.6% / 70.2%**. Minimum available RAM was **1.873 GiB**; recorded swap ranged about **797–852 MiB**, with **4,772 swap-in pages (18.6 MiB)**. This validates this bounded run, not unlimited operation or physical speaker behavior.

All 364 completed WAVs were finite with zero clipping; maximum internal sample step was **0.02051**. Ten deterministic Whisper samples scored **7.03% weighted WER**, median **1.43%**. The previously repeated first sentence had zero edits. All three previously failed inputs completed in the full pipeline: the loan fragment was 2.08 seconds, the normalized ellipsis 4.08, and “yes” 0.56. Targeted transcription found no repetition in these repaired endings, although recognition still confused some words in the full loan sentence.

A separate check of the three longest pieces found an additional defect in the longest: **“After winning 25,000, I am afraid to tell them it is gambling.”** automatically transcribed with **“I am afraid to tell them” repeated**. Its 10.64-second audio remained below the duration guard, showing that passing the guard does not establish quality. The other two long pieces matched their expected text. The repeated piece was not admitted in the original baseline, so there is no matched original WAV comparison for it; it is an observed quality issue, not proof of a baseline regression.

Fixed-reference clause controls kept every word and number and used the same seed 42/five steps/two threads. **“After winning 25,000.”** generated 2.16 seconds and **“I am afraid to tell them it is gambling.”** generated 2.64, totaling **4.80 seconds** versus 10.64. Cached transcription retained the number and found no repeated phrase; it rendered “I am” as the contraction “I'm”. Both variants were within the original limits. The trial does not prove that every numeric sentence needs this change or that all 608 pieces are free of repetition.

The next splitter gives written numerals additional length units (three digits count as two; four or more as four), allowing the measured three-word numeric clause to form a separate piece. This changes only boundary selection: all original text and digits remain intact. Admission estimates, the 24-second live budget, 12-second actual buffer, seed, steps, threads and playback speed are unchanged. Terminal commas become periods rather than producing comma-period sequences in synthesis input. Exact pre-v4 splitter and worker sources were retained and matched to long-v3's recorded hashes. **All 34 regression tests passed on both hosts** before exactly one short-v4 was launched.

Full long-v3 evidence, including the targeted quality issue, is collected locally. Verified **364 parent WAV hashes, 608 piece hashes and the combined hash**, plus ordered PCM assembly and original text/synthesis-word preservation. Transfer archive SHA-256: `4a0353cd81359e15e8881560e620597473f067bb47c4d0923351de247a4d1d65`. The retained [27:37.60 combined WAV](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v3/qiqi-translated-combined.wav) includes the flagged repetition; it is retained as evidence rather than presented as a fully corrected result.

## Passed short-v4 and numeric service control

Short-v4 retained identical recognition/translation to the controlled baseline and completed **21/22 valid jobs**, with **zero failures, expiry or overruns**. One skipped caption accounts for 39/367 words; completed-text coverage remains **89.4%**. It generated **41 pieces**, median **2.96 seconds**, maximum **5.76**, and a **121.68-second (2:01.68)** combined WAV. The combined file joins completed sentences and omits original pauses/skips; it is not a single synthesis call.

On 13 matched jobs, median first-ready delay changed **9.94 → 6.64 seconds** and median first-play **15.99 → 11.14**, maximum **23.95 → 20.28**. Minimum available RAM was **3.498 GiB**, median CPU **49.9%**, and swap-in five pages. All completed WAVs were finite with zero clipping. Nine cached Whisper samples again scored **4.55% weighted WER**, with zero edits on the previously repeated first sentence. Physical output and human voice similarity remain unverified.

The two-minute input does not contain the later 25,000 example. Therefore, after all workload/scoring processes exited, a separate **real SpeechService + production worker control** submitted that exact sentence with live chunking and download-only output. It completed as two pieces totaling **4.80 seconds**, and **both piece WAVs matched the already scored numeric controls byte for byte**. This tests the deployed splitter, punctuation, native options, worker and parent assembly without pretending the short microphone replay exercised the later source sentence. It did not test physical playback.

All short-v4 artifacts are local, with **21 parent/41 piece hashes**, the combined hash, exact PCM assembly and text preservation verified. Transfer archive SHA-256: `1bd24f6c809ede073b275fc6d2b67fba638074f0d915e9074bb4e344675c5ee5`.

- [Latest validated short combined WAV, 2:01.68](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/short-v4/qiqi-translated-combined.wav).
- [Representative two-piece numeric sentence, 4.80 seconds](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/numeric-service-v4/numeric-clause-service.wav), [production control evidence](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/numeric-service-v4/result.json).
- [Short-v4 matched comparison](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/short-v4/comparison.json), [long-v3 targeted transcriptions](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v3/targeted-intelligibility.json).

Only after these checks passed was **long-v4** launched once, with harness **862942**, supervisor **862943** and unchanged 2,160-second watchdog. Startup at 91.40 seconds showed ten completed jobs, no API error, and **3.645 GiB available RAM**. Scheduled validation must explicitly inspect the numeric source case and the three longest pieces after shutdown, as well as the ordinary sample. This is the second additional repair after short-v2; at most one further unresolved repair cycle remains before reporting the needed decision.


## Finished long-v4 and final repeated-clause repair

Long-v4 exited cleanly after **1,841.04 seconds**; harness/supervisor and test-owned workload services stopped and the workload lock was released before scoring. All **407 recognition/translation outputs match the original baseline**. Input delivered **90,000 frames, zero overruns**. It completed **365/379 valid jobs (96.3%)**, with **zero failures or expiry**, 14 budget skips, the same three overlength/19 unsupported-language rejections and six numeral-preservation translation rejections. Completed text contains **4,810/5,258 words (91.5%)**; skipped captions account for 448 words. This counts text, not verified pronunciation.

The 620 generated pieces have median/p95/maximum lengths **2.56 / 4.56 / 7.84 seconds**; median/max synthesis is **1.92 / 5.89 seconds**, and median RTF **0.762**. Eighty-one jobs began playback before parent generation finished. Maximum actual playback buffer was **10.32 seconds** and no successful synthesis was discarded or left unplayed. Combined completed output is **1,652.00 seconds (27:32)**, omitting original pauses, skipped captions and rejected translations.

On **230 identical matched completed jobs**, first-ready median improved **6.47 → 4.32 seconds**. First-play median improved slightly **10.62 → 10.34**, p95 **25.63 → 18.25**, maximum **29.84 → 20.87**. Overall first-play median was 11.88 seconds, maximum 22.40. CPU median/p95/maximum were **52.3% / 61.8% / 70.7%** across the recorded run. Pocket worker median CPU was approximately **2.28 cores**, maximum RSS **0.584 GiB**; recognition median approximately 0.50 core/max RSS 0.557 GiB; translator max RSS approximately 3.26 GiB (shared memory accounting is not a precise CPU/GPU allocation). Minimum available RAM was **1.902 GiB**; existing swap ranged **811–845 MiB**, with 680 swap-in pages (**2.66 MiB**). These bounded measurements do not establish unlimited operation.

All 365 completed WAVs were finite with zero clipping; 255 internal joins had maximum sample step **0.05505**. Ten deterministic cached Whisper samples scored **5.50% weighted WER**, median zero. The previously repeated first sentence had zero edits. Errors remain around names, monetary pronunciation and imperfect source translations; automatic ASR does not certify human voice similarity. Targeted case 242 retained 25,000 and had no repeated phrase in the full pipeline. All three earlier failure cases completed; targeted transcription still misrecognizes words in the loan sentence. No targeted cases were missing.

The third-longest piece, source case **124**, transcribed with one extra “a wave of unrest.” Its text already contains intentional repeats, which must be preserved. The exact 6.96-second waveform matched the same piece retained in long-v3 (and long-v2), so it is not a newly introduced numeric-boundary regression. Isolated fixed-reference controls reproduced that exact WAV/hash and extra phrase. Generating its three comma-separated clauses separately produced **1.20 + 1.68 + 1.68 = 4.56 seconds**, and automatic transcription then matched the exact intended phrase count. No waveform was trimmed or source repeat removed.

The final measured change splits adjacent identical punctuation-delimited clauses of at least three words when they share a piece. It retains all words, numbers, intentional repeats and ordering; unrelated short repeated words do not force fragments. Buffers, admission estimates, tempo, seed, steps, reference and threads are unchanged. **36 regression checks passed on both hosts**. The actual production SpeechService produced the repaired full sentence as four pieces totaling **7.92 seconds**; its three changed tail pieces match the scored controls byte for byte. Exact pre-v5 sources matched the long-v4 protocol and are retained under source-before/v5. **short-v5** then passed before the unique final long-v5 replay. This was the last allowed repair cycle; residual quality limitations are reported below rather than prompting further automatic inference.

Remote hash/text/PCM checks passed for **365 parent WAVs, 620 pieces and the combined WAV**, SHA-256 `35bf1165c5d6cce04c570dedda70a8e7b48554ee3fd7f8593af7b918eab84dd3`. Complete local collection is verified: transfer SHA-256 `c09ec06414c497b5ae0702bcd4fecbb68fe33d77fc5b2d65a66651719306a402`, all parent/piece/combined hashes, parent PCM equals ordered pieces, combined PCM equals ordered parents, and original/synthesis words are preserved. The long-v4 WAV is retained as evidence with the flagged repeat, not a fully corrected result.


Short-v5 exited cleanly after **148.44 seconds**, with **21/22 valid jobs**, zero failures/expiry, one 39-word budget skip, **6,000 frames/zero overruns**, and the same 328/367 completed words. All 24 recognition/translation outputs match the controlled baseline. Forty-one pieces had median **2.96 seconds**, maximum **5.76**; the combined **121.68-second** PCM/WAV hash is identical to short-v4. This short segment contains neither later repetition case, which is why their fixed-input production controls are separately required. Matched first-play median **15.99 → 11.25 seconds**, maximum **23.95 → 20.36**. Minimum RAM **3.564 GiB**, CPU median **48.7%**, swap-in **88 pages (0.34 MiB)**. After verified service shutdown, nine cached Whisper samples scored **4.55% weighted WER**, with zero edits on the previously repeated first sentence, all WAVs finite, zero clipping and maximum internal join step **0.005737**. Both later repair cases are covered by their separate fixed-input production controls. Only after short scoring exited was exactly one **long-v5** launched, with the unchanged 1,800-second input and 2,160-second watchdog. No inference overlaps the workload.


Short-v5 is also fully collected and locally verified: **21 parent WAVs, 41 pieces**, combined hash, exact ordered piece/parent/combined PCM and text/synthesis-word preservation. Archive SHA-256 `a9b02e805ebcda13bb6db4028a372a3708bab23c4b35cc48be7cc7d48c1580ef`. The repeated-clause fixed controls and production-service evidence are retained locally with hashes checked. There is no pending collection or quality inference.

The final **long-v5** startup check at **61.01 seconds** showed five completed jobs, no API error and **3.557 GiB available RAM**. Harness **876218** and supervisor **876219** own this single attempt. This startup observation is historical: final scoring and collection are complete below. No further automatic repair cycles remain; the residual quality flag is retained for a decision.

- [Retained long-v4 combined evidence, 27:32, includes flagged repeated tail](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v4/qiqi-translated-combined.wav).
- [Final short validation combined WAV, 2:01.68](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/short-v5/qiqi-translated-combined.wav).
- [Corrected four-piece sentence, 7.92 seconds](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/repetition-service-v5/repeated-clause-service.wav).
- [Long-v4 targeted transcriptions](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v4/targeted-intelligibility.json), [fixed repeated-clause controls](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/repetition-controls-v5/intelligibility.json), [short-v5 quality](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/short-v5/quality-summary.json).


## Final long-v5 outcome and verified collection

The final replay exited cleanly after **1,826.48 seconds**. All owned processes stopped and the workload lock was released before sequential ordinary/targeted quality scoring. The same **407 recognized and translated captions exactly match the original baseline**. Actual input was **90,000 frames / zero overruns**. It completed **365/379 valid jobs (96.3%)**, zero failures/expiry, and 14 admission skips. Completed translated text contains **4,810/5,258 words (91.5%)**, versus baseline **3,938/5,258 (74.9%)**; the 14 skipped captions account for 448 words. The same 3 overlength/19 unsupported-language translations and 6 numeral-preservation rejections remain. These were not silently admitted or dropped to inflate completion.

It generated **622 pieces**, median/p95/maximum duration **2.56 / 4.48 / 7.84 seconds**, median/max synthesis **1.91 / 5.72 seconds**, median RTF **0.762**. Eighty-two jobs began playback before complete parent generation. Actual playback buffer peaked at **10.46 seconds** and no successfully synthesized audio was discarded or left unplayed. Combined completed WAV duration is **1,649.60 seconds (27:29.60)**, joining completed sentences and omitting original pauses, skipped captions and rejected translations. This is not one generation call. The old 132.56-second example likewise combined 22 separate jobs.

On **230 identical matched completed jobs**, first-ready median improved **6.47 → 4.28 seconds**; first-play median **10.62 → 10.16**, p95 **25.63 → 18.26**, maximum **29.84 → 21.05**. Overall first-play median/max were **11.78 / 22.61 seconds** and ready-to-play waiting median/max **5.50 / 10.00**. Most of the demonstrated gain is coverage and shorter tail delay; median start latency remains about ten seconds.

Across recorded samples, aggregate CPU median/p95/maximum were **51.1% / 60.5% / 68.5%** across six cores. Pocket worker median CPU was approximately **2.28 cores**, maximum RSS **0.584 GiB**; recognition median CPU **0.50 core**, maximum RSS **0.641 GiB**; translation server maximum RSS **3.225 GiB**. RSS is not a precise shared-memory CPU/GPU split. Minimum available RAM was **1.847 GiB**; existing swap ranged **824–908 MiB**, with **775 swap-in pages (3.03 MiB)**. The Jetson did not reach sustained total CPU saturation in these samples, but this bounded run does not prove unlimited memory stability.

All 365 completed WAVs were finite with zero clipping, maximum peak **0.9800**. Across 257 internal joins, maximum adjacent-sample step was **0.05505**; this is not a human audibility judgment. Ten deterministic cached Whisper-small samples scored **5.50% weighted WER**, median zero. The earlier first-sentence repeat remained absent. Eight targeted checks included three longest pieces and all five known cases; none were missing. Case 242 retains 25,000 without the extra phrase. Case 124 has the exact intended three occurrences of “a wave of unrest,” completing in four pieces **7.92 seconds**, versus earlier two-piece **10.32 seconds**. Loan/ellipsis/“yes” cases complete, although some loan words remain misrecognized.

The newly third-longest piece, **“This person is called ‘Roufen’ (Roufeng).”**, transcribed as **“This person is called Rufan. Rufan. Rufan.”** in 6.16 seconds. The translated piece contains two written romanized forms; ASR reports three repetitions. The Chinese source calls the term 跑分, while the English translation calls it a person and supplies inconsistent romanizations. That translation error is upstream of TTS. This remains a **flagged possible extra romanized-term repetition**, not a human-confirmed pronunciation judgment. Numerical/currency pronunciation, name recognition and awkward source translations also remain imperfect. The repair budget has been exhausted; no further automatic source edits or inference reruns will be launched. The remaining decision is whether this quality is acceptable for a demo or whether a separate TTS/text-quality investigation is required before live reliance.

Remote verification passed **365 parent WAV hashes / 622 piece hashes / combined hash**, exact ordered parent/piece/combined PCM and preserved original text/synthesis words. Combined SHA-256 is `5bfcb95f4ba14d8380f8383722e9a2a54cd51167a11f94606f082a6fa7dd736c`. Exact final source files were retained with hashes matching the run protocol. Complete local transfer is now verified: archive **183,919,868 bytes**, SHA-256 `a2339072a051610ee06ca8e91b7d0ec8931382d3d778534c26b39393c1c85534`; all 365 parent/622 piece/combined hashes, exact ordered combined/parent/piece PCM, and original/synthesis word preservation passed locally. All seven retained final-source hashes match the run protocol. Physical speaker behavior, human reference-voice similarity and unlimited operation remain unverified.


The final v4/v5 comparison matched **all 365 completed source sequences**. Exactly one parent WAV changed: repaired case 124 (**10.32 → 7.92 seconds**). The other **364 parent WAVs are byte-identical**, including the first piece of flagged case 336 (SHA-256 `60b087a038c58332e7e010da54c3d30bc9efe7f2bcfd85ddab8260554f4583b3`). Thus the flagged romanized-term output was already present before the last repair; it was exposed by the new top-three quality selection. This does not make the flag acceptable or prove absence of untested problems.

The validated runtime improvement is usable as a prototype: shorter pieces, earlier playback while generation continues, much higher completed coverage, no generated-audio capacity loss, and zero observed input overruns. Further live reliance needs a decision about the remaining quality gaps. The next focused investigation should first review the flagged audio against its Chinese/translated text, then address the translation of terminology and controlled TTS pronunciation/repetition. Physical speaker pacing and human Qiqi similarity need listening tests; admission skips remain a separate capacity/coverage issue. The completed bounded run does not justify removing buffers or claiming indefinite memory stability.

- [Final combined WAV, **27:29.60**, completed sentences joined without original pauses/skips](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v5/qiqi-translated-combined.wav).
- [Representative corrected four-piece sentence, **7.92 seconds**](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v5/archive/bc71cc0a1bbf482f9f2dc1a2d0d7a427.wav).
- [Flagged romanized-term piece, **6.16 seconds**](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v5/archive-chunks/8711f1a08fdf4c1095f8c8aa0c883eb5-part-000.wav).
- [Actual run metrics](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v5/summary.json), [matched baseline comparison](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v5/comparison.json), [ordinary ASR samples](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v5/intelligibility-samples.json), [targeted ASR samples](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v5/targeted-intelligibility.json).
- [Local collection verification](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v5/collection-receipt.json), [transfer receipt](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v5/transfer-receipt.json), [v4/v5 waveform comparison](/home/moyamryia/Projects/Project-Mozart/rvc-golden/chunked-speech-20261006/long-v5/v4-v5-audio-comparison.json).

All 36 regression checks passed locally and on Jetson before the final short/long validation. Workload services remain stopped. All prior attempts and diagnostic controls are retained; unrelated source changes and remote commits were preserved. No commit or push was performed for this campaign. The scheduled follow-up is paused after delivery of these verified results.
