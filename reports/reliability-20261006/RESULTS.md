# Mozart reliability campaign — final short replay verified, quality limitations remain

The final120-second replay is completely collected and verified on both hosts. All20admitted sentences completed without failure, expiry or capacity rejection; four uncertain source captions were blocked before translation. Recognition and translation meaning remain insufficient for live reliance. The4B translator is not approved as the production default, and no new30-minute reliability replay was launched. All74regression checks passed on both hosts. Existing source changes and remote commits are preserved; nothing was committed or pushed. Earlier sections retain the historical sequence of controls and validation snapshots; the final outcome below supersedes their pending/running status.

## Implemented corrections

- A bounded four-entry FIFO holds text when speech capacity is temporarily occupied. It reserves no audio until admission, preserves the original 30-second first-play deadline, and keeps the 24-second estimated live budget and 12-second actual playback buffer. Queue order, cancellation and expiry are tested; synthesized audio is not discarded to make capacity fit.
- Live download-only jobs now stop first-output expiry at audio readiness. Playback jobs still require actual playback start. A real control reproduced the former expiry-after-audio bug before correction.
- The supervisor preloads the speech worker and confirms runtime readiness before capture. No artificial warmup sentences or extra audio enter coverage.
- Speech boundaries retain numeric ranges, nearby noun phrases and complete inventory-value clauses, while preserving existing punctuation boundaries. Original words, numbers and intentional repetitions remain intact. Previous frame guards remain active; waveforms are not trimmed.
- Translation retries start from the original source rather than a rejected answer. Checks reject missing/repeated/added large quantities, explicit goods values misread as item counts, untranslated Chinese and token-limit truncation. Calendar equivalents and explicit 万/亿 quantities are handled conservatively; malformed/composite quantities are not guessed. Source-grounded hints replace global unrelated examples. Every response, rejection and normalized model input is audited. These checks do not establish semantic correctness.
- Translation prompt cache is bounded at 128 MiB, with batch256/microbatch128. Optional processed source-utterance WAVs and ASR-emission/bridge-receipt timestamps support diagnosis. Recording is off by default and enabled for campaign replays. Source WAVs are16kHz PCM16 quantized from the processed ASR contract, not untouched microphone recordings.
- UI distinguishes waiting for speech capacity from active processing. The frontend passed type checking/build and was deployed to the Jetson.

All **57 regression checks passed on both hosts** before the full-stack replay. Tests cover queue bounds/order/cancellation/expiry, readiness, splitting and text preservation, numeric checks/retry behavior and port ownership. Existing RVC is disabled in these speech replays; no neural RVC changes were made.

## Measured isolated controls

Five simultaneous real PocketTTS jobs completed through paced ALSA null. The deferred fifth job waited17.16s for admission and began playing25.83s after submission, within the unchanged30s deadline. Actual buffered audio peaked9.90s. Downloaded WAV hashes and durations were verified locally. This demonstrates the fixed-input queue behavior, not continuous-input coverage.

| Candidate/control | Actual finding | Decision |
| --- | --- | --- |
| Existing0.8B Q4 | Reproduced unsupported repeated amounts; corrective chat history reinforced the bad answer | Retain failure evidence; add audited rejection and fresh retry |
| Existing0.8B Q8 | Improved numeric preservation but wrong roles/idioms/domain terms remained | Insufficient quality improvement |
| Qwen3.5-2B Q4 | Six clean semantic examples passed; original inventory value could become38,000items and domain errors remained | Do not adopt as default |
| Official Helsinki-NLP opus-mt-zh-en, CTranslate2 int8 CPU |17cases/8.45s, maxRSS664MiB; lost700/25,000/iPhone14, changed yuan to dollars and mistranslated turnover | Rejected despite speed/memory advantage |
| Tiny-clause translation fallback | Recovered some quantities but introduced role/negation errors | Rejected |
| Qwen3.5-4B Q4 | Six clean examples passed;9/11 difficult originals passed numeric/English guards; improved inventory value and withdrawal sentence | Candidate awaiting full-stack validation |

Selected4B GGUF is pinned to unsloth/Qwen3.5-4B-GGUF revision`e87f176479d0855a907a41277aca2f8ee7a09523`, SHA256`00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4`. Its corrected streaming-hash control retained **2.591GiB minimum available RAM** and measured3.820GiB maximum translator RSS. These are translator-only controls, not full-stack memory results; RSS is not an exact unified CPU/GPU allocation. Source297's malformed mixed numeral and394's truncated sentence remain rejected. Garbled source roles remain uncertain.

Earlier4B memory controls allocated the entire2.74GB model file in Python while hashing after server startup. Their0.35–0.49GiB minima include this diagnostic allocation and cannot be attributed to inference. Streaming hashing removed it. No-mmap, microbatch32 and disabled CUDA graphs did not address that allocation and were not adopted. All original controls remain retained.

## Final targeted synthesis controls

Five exact translations completed through the actual service/worker after the final boundary correction. Parent WAV hashes and durations were verified locally. Cached official Whisper-small produced these observations:

| Source | Duration/pieces | Automatic transcription finding |
| --- | --- | --- |
|156 |18.24s/6 |20,000–30,000 and38,000 retained; inventory-value clause intact |
|195 |9.28s/4 |Crocodile tears and May2022 retained; Oh omitted in transcript |
|242 |10.24s/3 |25,000 retained, no extra repeated phrase |
|336 |6.16s/2 |Exactly two intended account-provision occurrences, no romanized alias |
|396 |15.76s/6 |16,000/20,000/5times retained; gone/got discrepancy remains |

Controls-v4 retain the prior38,000→8,000 ASR flag and extra account-provision occurrence. Controls-v5 preserve their corrected outputs for comparison. ASR observations are not human pronunciation or reference-voice similarity approval. Prior failed outputs are retained without waveform trimming.

## Full-stack replay protocol and current state

Fresh input replays use seconds720 onward from`/tmp/test-audio.mp4` at microphone pace, the exact previous Qiqi source reference, PocketTTS int8 CPU/two threads/five steps/seed42, Zipformer14M plus SenseVoice final refinement, pinned4B GPU translation and paced ALSA null. RVC remains disabled. Original audio budgets/deadline/tempo are unchanged. Short input120s and long1800s; watchdog input+360s. Harness archives every generated piece and completed parent before history eviction, with source utterances, telemetry and hashes.

**short-v1 failed before input started.** Its launch command resolved the`.venv/bin/python` symlink to systemPython3.12, which lacked soundfile. No captions or speech were generated; owned services stopped. This was an invocation error, not measured inference overload. A reusable launch_attempt.py now preserves the virtual-environment interpreter path and validates dependencies before launch; run_test.py also checks dependencies before starting services. The failed directory remains untouched.

**short-v2** was the fresh120s validation under the correct environment; its completed failure and subsequent corrections are recorded below. No new long replay is launched until workload shutdown, collection/hash/PCM/source-span verification and relevant offline quality checks pass. Changed English translations will be inspected before comparing coverage or latency with the prior campaign; identical-text matches and source-level coverage are reported separately.

The new campaign permits up to three measured replay correction cycles. The existing scheduled follow-up monitors one active attempt, runs quality only after workload shutdown, repairs concrete failures, and validates a unique short before a unique30-minute replay. It never duplicates tests or reopens completed earlier attempts. The supervisor's production model default remains0.8B until the4B candidate passes full-stack quality/memory validation.

Physical speakers, human voice identity and unlimited memory stability remain unverified. Combined result WAVs join completed sentences and omit original pauses, skips and rejected translations; text completion is not proof that every word was pronounced correctly.

## short-v2 result — not accepted for a long replay

The workload exited cleanly after166.95s and all owned services stopped with the lock released. Actual120s input delivered6,000frames with zero overruns and24captions. All24Chinese ASR outputs match the earlier short baseline, while every English translation changed. Offered English text increased367→446words, so there are zero identical-text completed pairs for a direct speech-speed comparison. Completed15/24jobs, four expired and five received capacity429;13deferred jobs included nine completions/four expiries. Completed text370/446words82.96% versus baseline328/36789.37%, with different translated-text denominators. Source-caption completion15/24 versus21/24 previously. This is a validation failure, not a production model approval.

Fifty generated pieces median2.64s/max6.48s; synthesis median1.93s/max4.34s and job synthesis RTF median0.723. Actual output buffer max10.85s. Successful synthesis that was never played consumed6.38s of synthesis wall time across expired jobs; this must not be described as zero wasted synthesis. Median enqueue-to-first-play19.90s/max29.86s. CPU median43.66% across six cores; minimum available RAM1.447GiB, existing swap927–1,106MiB and16,297swap-in pages63.66MiB. These are bounded run observations, not proof of unlimited stability or precise GPU allocation. Memory did not trigger the768MiB guard.

Local and remote verification confirmed24source-WAV hashes/header/time spans,15parent and50piece WAV hashes, all ordered parent/piece/combined PCM and every original/synthesis word. Combined133.44s SHA256`7761228b20da584c4aff9dd6e91c517064f691eb2452a418025200bfc4d48a42`; it omits pauses/rejected/expired jobs and is not one synthesis call. Ten cached Whisper samples weightedWER4.26%, no clipping; this only evaluates synthesized English against the generated English. Source15's incorrect dog-poison wording can therefore score zero edits despite the wrong meaning.

Targeted source diagnosis confirms a recognition/translation limitation: both SenseVoice and independent cached Chinese Whisper leave source1 awkward; source16's amount differs110,000 versus10,000 between recognizers. Neither is a verified human transcript, so no amount is automatically substituted. Source10's clear loan-repayment phrase was mistranslated as taking out a loan, and source23 added a herself object unsupported by the Chinese fragment. A measured concise/fragment-preserving translation prompt control now replays all24exact captions plus eight clean semantic fixtures. Every source fact, uncertainty and intentional repetition must remain; no buffer/deadline/tempo changes or inferred source repairs are permitted. No further full replay is launched until these controls are inspected.

## Measured correction after short-v2

The admission estimate now includes deferred FIFO text when predicting first playback. Deferred text remains excluded from actual audio reservations, but is no longer excluded from the predicted work ahead of a new sentence. Known unlikely30-second starts receive explicit429 before synthesis. Expired jobs record their admission/synthesis/playback stage and deadline reason. Actual24s/12s budgets and30s deadline remain unchanged. This is a deadline/admission correction, not proven increased throughput: early rejection must still count as unspoken source coverage. All58regression tests passed locally and onJetson, including rejection before unnecessary synthesis when deferred text already consumes the projected deadline. A fresh real queue control remains required before another replay.

The4B concise/fragment-preserving prompt experiment reduced446→432words across the exact24captions but retained repayment/role problems and introduced awkward meaning. It was reverted; every result is retained under translation-4b-concise-v2. The original prompt and current guards are now being tested against the already-downloaded pinned2B model using all24captions plus eight clean fixtures (translation-2b-replay-v4). Model choice is unresolved; selected4B remains an unapproved candidate and the production default remains unchanged. No new long replay is authorized by a mere zero process-exit code. The scheduled continuation checks real queue behavior, candidate semantics and a fresh short replay before any30-minute run.

## Current confirmed fixes and active replay

All60regression checks now pass on both hosts. The actual4B repayment control passed four fixtures: repay a loan, borrow700to repay, not repaying, and ordinary borrowing. Its model input uses an audited equivalent phrase only for explicit还贷款; the original Chinese is retained. A narrow guard blocks the measured loss/inversion of repayment, without pretending to evaluate all semantics.

Real queue-controls-v2 completed all five original burst jobs after the projected-deadline correction; the fifth waited17.20s for admission, actual buffer max9.937s. All recorded local WAV hashes verified and the owned service exited before the next workload. The corrected guard therefore retains this previously valid deferral.

The smaller2B exact-caption control also offered446words and retained repayment, role and idiom errors before the explicit repayment fix. It is not selected as a throughput shortcut. No speculative model/memory changes were adopted. The fresh120s **short-v3** ran under the original4B candidate with the measured admission and repayment corrections, harness952867. It finished capacity-limited; the result and next measured correction follow below. The scheduled follow-up will verify/scoring after shutdown, make bounded measured corrections if necessary, and launch a unique30-minute test only when the short checks justify it. Repair cycle1of3 remains active; original and failed attempts are preserved.


## short-v3 verified result and recognition diagnosis

The replay exited cleanly after162.81s; owned services stopped and the workload lock released before all ordinary/targeted/source quality inference. It delivered6,000frames/zero overruns and24captions. Completed15/24, zero failures/expiry, nine explicit capacity429 rejections. All nine deferred jobs completed, with median7.25s/max17.78s admission wait. Completed360/442offered English words81.45%;82offered words remained unspoken. Chinese recognition matches the earlier24captions exactly, but every English translation changed from the0.8B baseline: there are no identical-text completed latency pairs. Source-caption completion remains15/24 versus21/24 in that baseline. Eliminating expiry and wasted successful synthesis is a reliability improvement; replacing expiry with earlier rejection is not increased throughput.

Forty-seven pieces median2.56s/p954.16/max6.48s, synthesis median1.875/max4.376s, medianRTF0.717. Max actual buffer10.763s. Successful synthesis not played:0s versus6.38s of synthesis wall time previously. First playback median17.09/max27.32s, ready-to-play median6.77/max9.06s. CPU median42.44% across six cores. Minimum available RAM1.549GiB; existing swap950.3–1,052.1MiB,5,971swap-in pages23.32MiB. These are bounded observations, not unlimited memory stability. Eight unique cached official Whisper-small English samples scored weightedWER4.85%, medianzero. All15parent WAVs were finite/unclipped; peak0.9154, maximum sample jump0.01868across32internal joins. ASR agreement with generated English is not evidence that the original Chinese meaning was preserved.

The complete30,130,167-byte archive SHA256`633c76965dc097d36203deaf16d794fb5827698e4cccb6b0eb3a0f7e79a3b9f4` is collected and verified locally. Verification confirms24source-WAV hashes/header/PTS spans,15parent/47piece hashes, exact ordered piece/parent/combined PCM and original/synthesis words. Eight pre-change source hashes match the protocol and are retained under source-final. Combined129.68s SHA256`107b00b5f6e951a95b943c463539d0b68ca888cf27b344dab7a994bfcb1fb16e`; it joins completed sentences and omits original pauses and rejected captions, rather than representing one TTS call. Collection/transfer receipts and retained quality outputs accompany the run.

The recognizer endpoint clock control measured100ms received-versus-decoded lag. Moving cuts to that clock did not reliably improve recognition or amounts and was not adopted. A second control re-created6,000native contract frames whose quantized PCM matches the retained source exactly, then delayed model endpoints while native VAD indicated speech. It blocked368active-speech endpoint checks and changed24spans to10, preserving all120s of PCM. Some phrases recovered, but formatted recognition fused the spoken numeral sequence into7005000, and word fragments/incorrect terms remained. This gating change was not adopted. Counts from different segmentation would not be valid throughput comparisons.

A fixed-audio SenseVoice control directly compared number formatting enabled/disabled. On source1 the readings are700 versus七千; source2 is500 versus千五千; source16 is11万 versus一一万. Disabling formatting globally would not establish the intended amounts: the latter readings are still ambiguous. Independent Chinese Whisper also disagrees with SenseVoice on source16. No human transcript is verified, so no amount is substituted.

## Measured numeric uncertainty correction — validation active

The deployed final recognizer now conditionally decodes the SAME captured PCM in literal-number mode when the final or streaming text contains an explicit amount/year of at least100. It retains the formatted caption plus both readings and quantity evidence in asr_numeric_audit. Conflicting inventories, ambiguous repeated numeral units, or a failed check retain the original caption and report recognition uncertainty before ANY translation or synthesis request; speech_skipped_reason is asr_numeric_uncertainty. Neither transcript is chosen as an automatic correction. Small counts, other recognition errors and agreement between two wrong readings remain outside this narrow guard.

The installed native per-stream use_itn override reuses one loaded SenseVoice model ([pinned official implementation](https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/v1.13.6/sherpa-onnx/csrc/offline-recognizer-sense-voice-impl.h)). An actual deployed control checked all24retained source spans. The three numeric cases1/2/16 were rejected before translation; literal readings byte-match the independently configured ITN-off control. Elapsed11.19s/maxRSS0.423GiB describes this recognizer-only control, not full-stack memory or human source correctness. All control inference exited. All70regression checks pass locally and onJetson, including equivalent amounts/ranges/years, intentional repeated amounts, unchanged nonnumeric inference, explicit check failure and rejection before translation.

Exactly one120s **short-v4** is now running, harness961371/supervisor961405, after these controls and regressions exited. Startup71.25s showed four completed jobs, no API error and1.526GiB available RAM. Repair cycle2of3. Original budgets24s/12s,30s deadline, tempo, seed, steps, threads and the production0.8B model default are unchanged. The4B candidate remains unapproved for production. A long replay is held until actual reliability/quality/capacity checks justify it. This numeric rejection must remain unspoken source coverage in every comparison; any apparent queue benefit from less offered text is not faster synthesis. Full control-audio collection is pending after the workload; no control/scoring should be rerun.


## short-v4 verified outcome and final bounded repair

The workload exited cleanly after168.84s and every owned process stopped with the lock free. Actual6,000frames/zerooverruns,24captions:18completed sentences, three capacity429 rejections and three numeric uncertainty blocks before any translation/synthesis request. Zero failure/expiry; all10deferred jobs completed (median10.35/max17.74s wait). All24archived source PCM hashes and Chinese captions match short-v3/baseline. Relative to the older0.8B baseline,23English outputs changed; only one completed identical-source/identical-English job remains, which is insufficient for a broad latency claim.

Of21valid offered jobs,18completed;341/364offered English text words93.68%, with23offered words budget-rejected. Counting ALL source captions,18/24completed75%;92.70/120s archived utterance-span coverage77.25% (spans include pauses, not correctly translated word coverage). Numeric-blocked captions remain unspoken coverage and are excluded from the offered-English denominator because no English was generated. The apparent word/queue improvement follows removal of three uncertain inputs and cannot be claimed as faster inference.

Forty-three pieces median2.64s/p954.88/max6.48s, synthesis median2.033/max4.706s, medianRTF0.718. Actual buffer max9.660s; successful synthesis not played0. First-play median16.51/max26.13s, ASR-final-to-play median19.52/max27.61s, ready-to-play median6.51/max8.99s. CPU median38.40% across six cores; Pocket median1.643cores/maxRSS0.566GiB, ASR median0.516core/maxRSS0.609GiB, translator maxRSS3.721GiB (not precise unified CPU/GPU allocation). Minimum available RAM1.525GiB, existing swap956.56–1,045.63MiB,8,536swap-in pages33.34MiB. Bounded run stability only.

All18WAVs are finite/unclipped, peak0.88165;25internal joins maxsamplejump0.01868. Nine ordinary cached official Whisper-small samples weightedWER3.39%,medianzero. Three longest-piece transcriptions contain no extra repeat; source17's generated Yan name is transcribed as Yeah. Sources1/16 are explicitly missing due to numeric uncertainty, not quality-approved. English ASR can agree with incorrect translated roles/terms. Source15 still produced dog poison/poisoner wording and a modal polarity conflict; source13's5-day plan conflicts with the streaming补贴reading; source19 changes the role toI and source23 adds herself. No human Chinese transcript is established.

Complete short-v4 archive28,728,708bytes SHA256`66f0d43bbc990cfa235b0799bac37d90c3e4a7dc0d7733a6e05437e452555d70` is collected and locally verified (231manifest files). Verification confirms24source hashes/header/PTS spans,18parents/43pieces, all original/synthesis words, exact ordered PCM, three blocks before translation and nine source-final hashes matching the protocol. Combined123.44s SHA256`0a0c1016b56d703edf2ac76f3c2bc01181d7e1b151a3a30b76634d7a184cc324`; joined completed sentences omit source pauses and rejected captions and are not one TTS call.

All earlier ASR controls are NOW collected:34,820,552-byte archive SHA256`b3d9d7ac20c70cdb369c251344679063f58734a82f4445f9d8b4b6af973d003c`,100manifest files verified. Local checks confirm every retained WAV/input hash, exact120s PCM assembly for both clock cuts and both endpoint modes,6,000contract headers and exact quantized contract PCM, and all ITN/numeric-control source hashes. No inference was repeated. Receipts accompany the original evidence.

A focused two-span control found source13 formatted5天 and literal五天 agree; source14's ordinary一天also agrees. Therefore lowering the numeric threshold would not fix this observed problem, and no numeric-threshold/source-term change was adopted. Both input hashes are locally verified. The source's intended phrase remains unverified.

The final measured correction compares strict可能/不可能 cues in the existing streaming/final readings. When both contain those cues but their polarity/counts disagree, it records asr_polarity_audit and blocks speech BEFORE translation, preserving both readings with speech_skipped_reason=asr_polarity_uncertainty. It makes no replacement transcript and expressly records that the endpoints may differ; other negation, pronouns, names and terms are outside this narrow check. Model-free replay of all24saved events flagged source15 and retained source13's matched cues. All74regression checks pass on BOTH hosts, including intentional modal repetitions and rejection before translator requests.

Exactly one final120s **short-v5** was launched after controls/regressions exited, harness966886. Repair cycle3of3 is the final permitted cycle; no further automatic repairs will be launched. Quality/capacity remain unapproved, and no new long run is justified by exit0 alone. Future post-shutdown scoring includes source13/15/17/19/23 plus all three numeric blocks; blocked/missing outputs will be reported explicitly. Source Whisper remains independent automatic evidence. Production model default, audio budgets,deadline,tempo,threads/steps/seed and ASR cuts are unchanged.

## Final short-v5 outcome — locally and remotely verified

The workload exited cleanly after160.76s. All owned workload processes stopped, the workload lock was released, and ordinary English, targeted English and Chinese source scoring completed sequentially after shutdown. No inference was repeated during collection. All74regression checks passed locally and onJetson. The three permitted measured replay repair cycles are exhausted; the campaign ends with the following explicit quality limitations rather than another automatic repair loop.

| Measurement | Actual final result |
| --- | --- |
| Input |120s,6,000frames,zero overruns,24source captions |
| Recognition blocks before translation/synthesis |Four: three numeric conflicts and one possible/impossible conflict |
| Valid offered/admitted/completed |20/20/20jobs;319/319offered English text words |
| All-source caption completion |20/24=83.33% |
| Completed source-span coverage |88.86/120s=74.05%; spans include pauses |
| Capacity rejection/failure/expiry/partial failure |Zero each |
| Deferred jobs |Four/four completed; wait median10.56s,max16.15s |
| Generated pieces |41; duration median2.64s,p954.88s,max6.48s |
| Piece synthesis |Median1.90s,max4.68s; median jobRTF0.734 |
| First playback from submission |Median15.06s,max26.13s |
| ASR-final emission to first playback |Median17.09s,max27.31s |
| Bridge receipt to first playback |Median15.98s,max27.27s |
| First audio readiness / ready-to-play queue |Medians8.89s/6.18s; maxima18.57s/8.98s |
| Playback before parent generation finished |Seven jobs |
| Actual playback buffer |Maximum9.90s within unchanged12s limit |
| Successful synthesis not played |Zero seconds |
| CPU across six cores |Median41.64%,p9565.44%,maximum69.82% |
| Pocket / recognition process |Median1.642/0.485cores; maximumRSS0.575/0.562GiB |
| Translator process |MaximumRSS3.763GiB; not exact unified CPU/GPU allocation |
| Minimum available RAM |1.570GiB |
| Existing swap / swap-in |962.63–1,037.18MiB /4,330pages=16.91MiB |
| Combined generated audio |115.12s=1:55.12, from20separate jobs |

The all-source denominator includes the four blocked captions. They have no generated English and are therefore absent from the offered-English denominator. Completing all admitted text is **not**100%source coverage or semantic correctness. Reduced offered work relieved queue pressure; these results do not establish faster inference. All24source PCM hashes and Chinese captions match short-v4. Compared with the older0.8B short baseline,23English outputs differ and only one completed identical-source/English pair remains, which is insufficient for a broad speed comparison. CPU was not saturated throughout this short run, but long-run4B memory stability is unmeasured.

Sources1/2/16 preserve the numeric conflicts700 versus七千,500 versus ambiguous千五千, and11万 versus ambiguous一一万. Source15 preserves streaming可能 versus refined不可能. Neither amount nor polarity was guessed or corrected. Parser interpretations of ambiguous numeral strings are diagnostic evidence, not verified intended amounts. The numeric check uses the same PCM; streaming/final polarity readings have imperfect endpoint alignment. Guard agreement cannot prove source truth, and other pronouns, negation and terms remain outside these narrow checks.

The complete27,028,125-byte archive SHA256`bf859f023e276b062ba7038a67bde806a6fad93d119d1b4976c62e815f1c6ab0` and downloaded checksum match. All238manifest files were checked before derived receipts were refreshed. Local verification confirms24source WAV hashes/mono16kHz PCM16headers/PTS spans,20parent and41piece hashes, exact ordered piece/parent/combined PCM, all original/synthesis words, and all nine retained protocol source hashes. Blocked captions contain no translation result/audit or speech request. The combined SHA256 is`9bdfc060eb9fadd753f6bebc5892d8c2e68dacb5e077d221545eb979609b77ac`. Transfer, collection, shutdown and matched-audio receipts accompany the evidence.

## Final quality findings and audition evidence

All20parent WAVs are finite and unclipped, maximumpeak0.90256. Across21internal joins the maximum sample jump is0.01868. Nine deterministic cached official Whisper-small English samples scored weightedWER7.69%,median7.14%. The selected samples differ from short-v4's sample set. All17matched completed jobs with identical source/English have **byte-identical parent and piece WAVs** in both runs; the WER change does not establish a waveform regression. One sample, source8, transcribes exactly against the generated English. Other samples include principal/interest, scamming/scanning, and dropped or changed small words. These are automatic transcription observations, not human pronunciation approval.

Seven targeted checks cover the three longest pieces and sources13/17/19/23; no extra repeated phrases were detected in these samples. Sources1/2/15/16 are explicitly missing due to recognition blocks, not quality-approved. Eight independent Chinese Whisper checks remain automatic evidence, with no verified human source transcript. Specific unresolved failures include:

- Source19's Chinese includes他不管对面是谁, while the English continues withwhether I'm there. This changes a third-person role and interprets an unfinished clause. Independent Chinese Whisper also retains他; it does not establish a full human transcript.
- Source23 says妈妈已经有点不相信了妈妈, while the English addsunsure of herself. The reflexive object is unsupported by the retained fragment. Its English Whisper transcript has zero edits, demonstrating why English intelligibility is not translation accuracy.
- Source17's fragment严 becomes the person/nameYan; the previous span ends with要跑. The intended joined phrase is unverified. Source13's5-day plan also remains uncertain despite formatted/literal agreement. These need source-level review and segmentation evidence rather than guessed replacement words.
- Numeric sources disagree across recognizers, and source15's term毒狗 remains unverified. No amount or homophone was silently substituted.

Audition files below are absolute local paths. The combined WAV joins completed sentences and omits original pauses and four rejected source captions; it is not one TTS call. The earlier132.56s result likewise combined22jobs.

- [Final combined audio,1:55.12](/home/moyamryia/Projects/Project-Mozart/rvc-golden/reliability-20261006/short-v5/qiqi-translated-combined.wav)
- [Representative source8 sentence,13.28s/four pieces](/home/moyamryia/Projects/Project-Mozart/rvc-golden/reliability-20261006/short-v5/archive/d8a2997fd5f14aff91be1a0275a6adb9.wav): pieces2.00+3.92+3.52+3.84s, coveringambition/gambling/online loans/online dating/meeting in person. Every generated English word is preserved; automatic English transcription had zero edits, without human semantic approval.
- [Source19 Chinese,9.60s](/home/moyamryia/Projects/Project-Mozart/rvc-golden/reliability-20261006/short-v5/runtime/utterances/0019.wav) and [problematic English,11.12s/five pieces](/home/moyamryia/Projects/Project-Mozart/rvc-golden/reliability-20261006/short-v5/archive/5d63ac234dd442269299a368c00c5925.wav)
- [Source23 Chinese,2.56s](/home/moyamryia/Projects/Project-Mozart/rvc-golden/reliability-20261006/short-v5/runtime/utterances/0023.wav) and [unsupported reflexive English,2.56s](/home/moyamryia/Projects/Project-Mozart/rvc-golden/reliability-20261006/short-v5/archive/cf0cfa9456f24a7f9a1fce57f4757977.wav)

Paced ALSA null is not physical speaker testing. Human reference-voice identity and exact pronunciation are unverified. The bounded120s run does not establish unlimited memory stability. The4B translator remains unapproved and the production supervisor still defaults to0.8B; retaining that default is not an endorsement of its quality. Source audits and queue reliability improved, but speech still contains meaning errors. No fresh30-minute reliability replay was launched.

## Next reliability redesign, in order

1. Establish a reviewed Mandarin transcript and faithful English reference for the retained120s excerpt, prioritizing amounts, possible/impossible, pronouns and gambling/course terms. Mark genuinely unintelligible spans rather than inventing a transcript. This is the required reference for a credible source-quality score; another automatic recognizer is insufficient.
2. Evaluate an utterance assembler against that reference. Preserve continuous PCM and source timestamps, use bounded context across cut fragments, and compare streaming/final recognition on aligned spans. The tested100ms clock shift and VAD endpoint gating were rejected because they did not reliably improve quality. Any new boundary design must measure amount/role/term errors, latency, dropped frames and RAM together.
3. Evaluate translation separately on the reviewed Chinese. Make pronoun roles, negation, explicit/implicit objects, amounts, fragments and domain terms required cases. Preserve uncertainty and audited original text. Keep4B as a candidate until it passes source meaning and full-stack capacity; do not assume a larger model, narrower guard or EnglishWER resolves these failures.
4. Retain the bounded streamingTTS/queue and matched-input regression evidence. Evaluate pronunciation and reference identity by listening alongside original/reference audio; use automatic transcription only as supplementary evidence. Do not trim waves, remove clauses, increase tempo or enlarge budgets to pass.
5. Run a new unique short only after a measured redesign passes source/translation controls. Require explicit all-source coverage and honest rejection counts, then a sequential30-minute run and physical speaker check. Do not resume an unlimited automatic repair loop.

The scheduled campaign is paused after this report. The next stage needs reviewed source/reference evidence and a separate measured recognition/translation redesign; no demo-versus-reliability decision is being reopened.
