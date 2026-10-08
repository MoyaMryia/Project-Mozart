> 历史记录：本文保留当时的测量、日志与审查原文。当前部署和接口以[维护文档](../../README.md)为准。

# Longer Mozart speech test — October 6, 2026

Status: completed on `jetson-wg`, collected and verified. See [final results](RESULTS.md).

This run replays seconds 720–2520 (minutes 12–42) of the supplied `test-audio.mp4` through the native preprocessing/UDP microphone path at wall-clock pace. The speech-only configuration uses SenseVoice final refinement, local Qwen translation and PocketTTS with the saved Qiqi reference. ALSA null playback is paced to generated WAV duration. This does not test acoustic output or human voice identity.

The harness has a 36-minute watchdog, archives completed WAVs before the service evicts history, and writes a combined output plus per-sentence source/translation manifest. The combined file omits original pauses and skipped sentences. Runtime code is unchanged during this baseline run.

The scheduled follow-up `mozart-longer-speech-test-results` checks every ten minutes, remains quiet while healthy, and collects and scores results after workload shutdown. Final results will be written to `RESULTS.md` in this directory. It will pause after reporting results or a concrete failure requiring intervention.

Remote artifacts: `/home/moyamryia/Mozart/rvc-golden/long-speech-20261006`. Local collection destination: `/home/moyamryia/Projects/Project-Mozart/rvc-golden/long-speech-20261006`. The detached harness is `run_test.py`; `score_outputs.py` is for offline scoring after the timed services exit.
