> 历史记录：本文保留当时的测量、日志与审查原文。当前部署和接口以[维护文档](../../README.md)为准。

# Mozart mock demo — 7 October 2026

The app is running on Jetson through the local control page http://127.0.0.1:5180/. The current run uses `/tmp/test-audio.mp4`, original seconds 720–840, at microphone pace, and a silent ALSA null speaker paced to each generated WAV's duration. USB capture and playback are disabled in this run. The user previously reported completing the physical audio check; that is user evidence, not an independently repeated speaker test.

The two-minute mock input has finished. The API, selected Qiqi voice, translator and preview worker remain available; capture and ASR have ended. This is visible as no incoming VAD stream. The new opt-in `--keep-open` flag retains services after file replay and preserves the existing 768 MiB memory guard and child-process checks. Normal file tests still exit by default. The interface has an explicit silent mock-speaker label and hides inactive RVC controls in the speech-only configuration.

## Checks and retained evidence

- All 85 regression checks passed on both hosts. The frontend type check and production build passed after the UI edits.
- Model selection is an explicit demo override: the pinned Qwen3.5-4B model hash was streaming-verified before startup. The production default remains unchanged. PocketTTS uses the existing int8 model and Qiqi reference; RVC remains disabled.
- Startup and shutdown/restart worked. The first owned stack released all nine owned processes and all four service ports. A subsequent brief USB capture startup was stopped when the user requested mock-only audio. Only the current file-input mock stack remains active. Exit logs and ownership receipts are retained.
- Voice registration and selection, unsupported-language/missing-voice/empty-text rejection, four uncertainty controls before translation, cancellation of an active and a queued job, and successful preview generation after cancellation passed. The two functional preview WAVs were hash-verified locally.
- The visible UI generated a 4.80-second preview of “You are gambling, you are borrowing online loans, you are dating online.” It completed through the silent paced speaker. The UI stop control disabled speech, and its enable control restored the same Qiqi session. The app is left enabled with mock output. This preview's SHA is `f2f14990e8c30c6b853b3b85e689ff0beb6999aec95dc2bc4e019735e461caff`.
- Live hardware status, API connection and caption SSE were observed in the UI. The saved screenshot shows the final selected voice, silent output and completed preview.

## Actual mock replay

The preprocessor sent **6,000 frames / 120.0 seconds, zero overruns**. There are **24 source captions**. **18 new speech jobs completed**, with no failed, cancelled or expired replay jobs. Six source passages were explicitly withheld: 1/2/16 for numeric uncertainty, 15 for polarity disagreement and 19/23 for unresolved boundaries. Those are unspoken coverage, not successful translations. Prior functional jobs are excluded from these replay counts.

All 18 parent WAVs and 35 generated pieces are collected and verified locally. Ordered piece PCM exactly matches each parent, and ordered parent PCM matches the combined file. Original/synthesis word sequences are retained. All WAVs are finite and unclipped; maximum normalized peak is 0.902557. All 24 recognition/translation pairs and all 18 parent WAV hashes match the earlier sentence-context short-v2 evidence: this launch did not improve or alter source quality.

The combined WAV is **101.44 seconds (1:41.44)**, SHA `8b39375dbf162a4eaedea4b8f3941a7be454556bddb0f47146fb987178df2143`. It joins completed sentences and omits original pauses and withheld passages. It is not one synthesis call.

Files are under `rvc-golden/demo-20261007/mock-v1/`; `verification-receipt.json` records the exact audio and text checks. The post-replay available-RAM snapshot was 2,115,469,312 bytes, approximately 1.97 GiB. This is not a measured minimum during workload or an unlimited memory-stability result. The previous campaign contains full-stack CPU/memory telemetry.

## Source meaning and demo scope

The Chinese reference is the user's explicitly confirmed 24-passage submission. Four clean text controls were drawn from it. Three retained their key facts: “This is you”; adding money/doubling/repaying the loan; and gambling/online borrowing/online dating. The loan English is awkward but preserves repayment. The bare “你看” control returned “You look”, an awkward incomplete fragment; the ordinary punctuated replay caption “你看。” returned “Look.” These controls exercise text translation, not new recognition accuracy or human voice similarity.

Known source-quality problems persist in the full replay, including the incorrect recognition of 我儿子 as 但是嗯现在, 补天计划 as a five-day plan, an isolated 考研 syllable as the name Yan, and fragmented 不当成伤害. Other pronoun/context errors remain. The guards withhold some uncertainty but do not certify all spoken meanings. No reviewed Chinese was silently substituted into the replay to make it look correct.

This is a functioning demonstration of the pipeline and reference-voice generation with selected checked sentences. Faithful translation of the complete input remains unfinished. Human voice identity and precise pronunciation are not approved by waveform checks; the user's physical hardware check does not establish that every translated sentence is correct.

## Operating state

The frontend listens privately on Jetson loopback port 5173, reached through the SSH forward at local port 5180. The mock stack owner is recorded in `rvc-golden/demo-20261007/mock-v1/pids.json`; the shared test lock is held to prevent another workload being launched concurrently. There are no active quality-inference or transfer jobs. Closing the browser does not stop the Jetson services. “Stop speech and clear queue” disables the speech session and cancels queued jobs; it does not shut down the application.

To replay the input again, stop the current owned supervisor cleanly before launching a new unique mock run. Retain this run's artifacts. No commit or push was made, and unrelated source changes were preserved.
