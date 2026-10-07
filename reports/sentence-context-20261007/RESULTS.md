# Source-context and explicit-meaning reliability work

The user authorized this stage with “go and make your decision.” The decision is to retain the bounded speech queue and add measured explicit-source correctness checks, while rejecting blanket endpoint merging. Source changes and prior campaign evidence are preserved; nothing is committed or pushed.

## Context experiment

Six joined windows reuse the exact processed source PCM from the completed two-minute replay. All six WAV hashes and ordered PCM assembly are verified locally. The experiment loaded one resident SenseVoice model, completed in 10.08 seconds and used at most 0.430 GiB process RSS; these are recognizer-only measurements. The largest diagnostic window was 18.88 seconds under an explicit 20-second experimental ceiling. Production cuts, audio budgets and capture remain unchanged.

Joining 7/8 recovers 想上进, 16/17 recovers 考研 instead of the isolated 严/name fragment, and 19/20 completes 在做什么事情. However, 1/2 fuses two amounts into 7005000; the numeric conflict remains rejected. The 11万 versus 一一万 conflict also remains. Joining 12/13 removes the apparent 5-day numeral but still leaves an uncertain term, not a verified correction. Waiting for the next endpoint adds 1.82–11.84 seconds across these windows. Blanket merging is rejected: contextual improvement in some words does not justify wrong amounts or that delay.

## Measured implementation corrections

A fresh authored Chinese fixture exposed a numeric validation bug: 七万五千 was parsed as separate 70,000 and 5,000 amounts, and the original translator answered 70,000 while the guard accepted it. Exact Chinese composite quantities now normalize atomically (75,000; 150,000,000; 108,000), both for prompting and validation. Repeated explicit amounts require matching occurrence counts. Ranges, stutters, mixed or colloquial shorthand remain unnormalized; no uncertain recorded amount is corrected.

Narrow source-grounded prompts and audits now preserve explicit 他不管 as third person and reject adding herself to the measured mother-doubt construction without 自己. These are checks for reproduced failures, not general coreference or semantic proof. Original source text is retained. Rejected answers cannot enter speech, and retries still start from original source rather than the rejected answer.

The exact 4B model/configuration was held fixed in two sequential ten-case controls. The authored amount fixture changes from 70,000 to 75,000. The retained source19 changes from whether I'm there to a third-person he clause, but its unfinished clause is still interpreted. Source23 changes from unsure of herself to doubtful of Mom: the reflexive error is removed, but the malformed source still yields awkward object attachment. The eight authored fixtures now preserve their specific amount/person/reflexive/modal facts; this is not a human-reviewed transcript of the recording or complete translation-quality approval.

All 81 regression checks pass on both hosts, including exact composites, ambiguous shorthand, repeated amount omissions, role/reflexive failure rejection and existing queue/port ownership checks. The production translator default remains 0.8B; the test candidate remains the pinned 4B model. Runtime limits, tempo, seed, steps, threads and ASR endpoints are unchanged.

## Source benchmark and review

A complete 120-second processed source WAV and 24-row reference draft are retained with SHA256 provenance. No row is labeled human-reviewed. The local review page plays continuous source audio with surrounding context and exposes streaming/final Chinese, generated English and uncertainty warnings. It exports reviewed Chinese, faithful English and unclear-span notes. The evaluator refuses automatic draft references, verifies source/WAV/boundary identity, measures Chinese character error and explicit quantity inventory, and does not claim automatic translation semantic approval.

- [Source review page](/home/moyamryia/Projects/Project-Mozart/rvc-golden/sentence-context-20261007/source-review.html)
- [Continuous processed source, 2:00](/home/moyamryia/Projects/Project-Mozart/rvc-golden/sentence-context-20261007/processed-source-120s.wav)
- [Unreviewed reference draft](/home/moyamryia/Projects/Project-Mozart/rvc-golden/sentence-context-20261007/source-reference-draft.json)

A reviewed source reference is still needed before measuring actual amount/role/term accuracy. Agreement between automatic recognizers is not human truth.

## Full-stack validation

Exactly one fresh short-v1 replays the same 120-second input at microphone pace, with Qiqi, PocketTTS int8/two CPU threads/five steps/seed42, Zipformer plus SenseVoice, pinned 4B GPU translation, RVC disabled and paced ALSA null. The original shared workload lock prevents overlap with older controls; the watchdog remains input+360 and RAM guard 768 MiB. Startup at 50.97 seconds showed two completed jobs and 1.516 GiB available RAM. Final outcomes follow below; no new long test is launched from process exit alone.

## First replay result and final measured correction

short-v1 exited cleanly after166.95s: 6,000frames/zero overruns, 24source captions,20/20valid jobs complete and321/321offered English words. Four earlier recognition blocks remain; all-source caption coverage20/24=83.33%, source-span coverage88.86/120s=74.05% including pauses. Seven deferred jobs completed; median wait8.53s/max16.16s. No failure, expiry, capacity rejection or successful synthesis left unplayed. Forty-one pieces median2.64s/max6.48s; synthesis median1.95s/max4.70s, median jobRTF0.732. Maximum buffer10.21s, first-play median15.18s/max26.13s and ASR-final-to-play median17.31s/max27.31s. CPU median39.59% across6cores; minimum available RAM1.485GiB, existing swap938.43–1,040.78MiB,9,454swap-in pages36.93MiB. These are bounded observations, not long-run stability or precise GPU allocation.

All owned workload PIDs stopped and lock released before sequential scoring. All20WAVs finite/unclipped, peak0.90256,21joins maxsamplejump0.01868. Nine ordinary English samples weightedWER9.49%; altered English makes broad comparisons invalid. Targeted source19 transcription renderedregardless asFor God's sake, a possible pronunciation/recognizer discrepancy rather than human confirmation. Its source fragment was still being completed. Source23 spokeMom is doubtful of Mom, retaining a poor object attachment. The first replay is retained as quality-limited, not approved from exit0.

The final measured correction flags exactly those two unresolved source boundary forms before ANY translator/TTS request. The original Chinese and uncertainty evidence remain in captions; no replacement transcript is chosen. Same-input replay of all24saved events flagged only19/23. Complete clauses and explicitly reflexive mother statements continue to pass regression checks. All83checks pass on BOTH hosts. This is a narrow protective gate, not a validated utterance assembler or general semantic evaluator.

One unique final short-v2 is running under the original limits, after all first-replay scoring exited. Repair cycle2of2 is the stage limit. Additional blocks will reduce coverage and will be reported as unspoken input, not throughput improvement. No long replay or larger model adoption will follow without source-quality evidence.

## Final short-v2 outcome

The final replay exited cleanly after156.72s. All18admitted sentences completed; no runtime failure, expiry, capacity rejection, partial failed job or successful synthesis left unplayed. Input remained120s/6,000frames/zero overruns and24captions. Six source captions were explicitly withheld:1/2/16numeric conflicts,15polarity conflict,19/23unresolved boundary forms. Both new blocks retain original text and audit evidence and make no translator or speech request. This leaves18/24=75%source-caption completion; lower offered work is not improved throughput or validated translation coverage.

There are35pieces, duration median2.64s/p954.96s/max6.48s; synthesis median2.20s/max4.79s, median jobRTF0.737. Seven jobs play before their parent finishes generation. The actual buffer peaks10.39s within the unchanged12s cap. All four deferred jobs completed, median10.67s/max16.28s admission wait. First-play median8.96s/max26.24s, readiness median5.42s/max18.72s, ready-to-play queue median5.29s/max9.02s. Reduced input to synthesis confounds any speed comparison.

CPU median34.17% across6cores. Minimum available RAM1.599GiB, existing swap948.03–1,051.01MiB and7,529swap-in pages29.41MiB. This is bounded short-run behavior, not unlimited or 30-minute4B memory stability. Human source correctness, pronunciation, reference-voice similarity and physical speakers remain unverified. Output is paced ALSA null.

The final combined WAV is101.44s=1:41.44, SHA256`8b39375dbf162a4eaedea4b8f3941a7be454556bddb0f47146fb987178df2143`. It joins completed sentences, omits original pauses and six withheld captions, and is not one generation call. First-replay115.28s WAV and every failed/ambiguous control remain retained for comparison. No waveform was trimmed, source words rewritten, audio buffer increased or tempo changed. Audio-quality and complete local transfer verification are finished and recorded below.

## Complete collection, quality limits and final decision

Both full-stack attempts are now collected and locally verified. short-v1 archive27,049,923bytes SHA256`5672c8d62257a0b34c1d2bd6a7744c041269be1881dddee8452ba8c7aaf29973` has236verified manifest files,20parents/41pieces and nine protocol-matched source snapshots. short-v2 archive24,150,497bytes SHA256`f049d165035114199ac4cf8e201167f390e9aac54c5ceecdbd4d3bc3757a2b2a` has216verified manifest files,18parents/35pieces and nine protocol-matched source snapshots. Both downloaded checksum manifests, combined hashes, exact ordered piece/parent/combined PCM, original/synthesis words and24source-WAV hashes/headers/PTS spans passed. All six final uncertainty captions contain no translated English, speech request or translation attempt. Every original source caption is retained.

Final offered/admitted/completed English text is274/274words over18jobs. All-source completion is18/24captions75%; source-span coverage76.70/120s63.92%, including pauses and not correctly translated word coverage. The first replay offered321words with20completions; losing two sentences is explicitly missing coverage, not improved capacity or faster synthesis. ASR-final-to-first-play median12.31s/max27.41s and bridge-receipt-to-play median11.57s/max27.38s are confounded by this lower load. CPU p9564.61%/max75.69% across6cores; Pocket median1.284cores/maxRSS0.556GiB, recognition median0.500core/maxRSS0.534GiB, translator maxRSS3.750GiB. RSS is not an exact unified CPU/GPU allocation.

All18final WAVs are finite/unclipped; peak0.90256,17internal joins maxsamplejump0.01868. Nine ordinary cached official Whisper-small samples have weightedWER6.77%,median10%. Sample composition differs from prior attempts and two problematic inputs were withheld; this is not an accuracy improvement claim. All18matched parent WAVs are byte-identical to short-v1. The three longest pieces and sources13/17 were transcribed; the six blocked sources are explicitly missing, not quality-approved. The prior source19regardless/For God's sake flag and source23object error remain in the retained first attempt. Sources13/17 still show plan/name ambiguities, so passing the protective checks does not make the remaining translations trustworthy in general.

All workload services were confirmed stopped with the shared lock free before English quality inference. Sequential ordinary/targeted quality completed. The exact same24source-WAV hashes and Chinese captions allow reuse of the retained eight independent Chinese Whisper observations; a provenance receipt records their origin/hash and that no new source inference was performed. These observations are not human source truth. No inference overlapped with a workload.

The self-contained review page was visually checked:24passages,72editable fields,120s embedded source audio andzero rows marked reviewed. It works from the saved HTML without needing the preview server. The evaluator correctly refuses the unreviewed draft. A reviewed source transcript/English reference remains the next required evidence before evaluating a bounded utterance assembler. Blanket merging, a larger translator, extended buffers/deadlines and faster tempo were not adopted. Source and remote changes remain uncommitted/unpushed. The production model default remains0.8B; the4B candidate is not approved for live semantic reliability. Physical speaker output, human voice similarity and long-run memory stability remain unverified.

Final decision: retain the measured exact-quantity and source-role corrections plus explicit unresolved-context warnings. Keep the original endpoints and smallTTS pieces. Stop automatic repairs after this stage's two measured cycles; no new long replay until a reviewed benchmark establishes recognition/translation meaning. A future assembler must preserve all PCM/text, recover fragments with bounded added latency, and pass quantity/role/term cases before full-stack testing. The review page is the concrete next artifact, rather than another uncontrolled inference loop.

- [Final generated audio,1:41.44](/home/moyamryia/Projects/Project-Mozart/rvc-golden/sentence-context-20261007/short-v2/qiqi-translated-combined.wav)
- [Representative four-piece sentence,13.28s](/home/moyamryia/Projects/Project-Mozart/rvc-golden/sentence-context-20261007/short-v2/archive/d26ff566afd54621807cfd5260641580.wav):2.00+3.92+3.52+3.84s; every generated word retained, without human semantic/voice approval.
- [Retained first-attempt output,1:55.28](/home/moyamryia/Projects/Project-Mozart/rvc-golden/sentence-context-20261007/short-v1/qiqi-translated-combined.wav)
- [Offline source review page](/home/moyamryia/Projects/Project-Mozart/rvc-golden/sentence-context-20261007/source-review.html)


## User-confirmed source review

The user confirmed all 24 submitted Chinese passages in chat: “Yes I reviewed, result is what I gave”. This supersedes the partial checkbox flags. Exact wording, amounts, source hashes and original submission are retained in `source-reference-moya-confirmed-v2.json`; reviewer is MoyaMryia. The reviewed Chinese is now available for benchmarking. English suggestions remain assistant drafts, and historical run outputs have not become quality-approved. No new inference or replay was run.

See `SOURCE-REVIEW-MOYA-CONFIRMED.md` for the organized confirmed text and the saved recognition/meaning differences. `source-reference-moya-confirmed-v2-comparison.json` compares all 24 same-hash saved segments; character disagreement is diagnostic and is not semantic translation scoring.
