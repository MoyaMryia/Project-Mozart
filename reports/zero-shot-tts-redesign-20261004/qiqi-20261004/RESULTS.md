> 历史记录：本文保留当时的测量、日志与审查原文。当前部署和接口以[维护文档](../../../README.md)为准。

# Qiqi reference-conditioned TTS — Jetson check, 2026-10-04

**The existing Qiqi Golden verification passes. Cloning its saved audio fits the Jetson, but English voice fidelity is not ready to approve. ZipVoice's Chinese output is the more promising result.** Pocket remains the English performance prototype, rather than a validated choice for every reference voice.

## What was tested

The project has `qiqi_zh` trained RVC assets and `qiqi-zh`/`qiqi-zh-realtime` test profiles. Pocket and ZipVoice cannot load an RVC checkpoint as a voice profile; their conditioning input is audio. This test used two saved Qiqi **original-PyTorch RVC Golden outputs**, not original game/actor recordings:

- An **8.00 s** crop of `qiqi-zh-espeak-pinyin-mixed-python-reference.wav`, produced from the retained deterministic espeak input.
- A **7.72 s** crop of `preprocessor-sample-mp4-30s-python-reference.wav`, produced from a real speech source. The crop ends at a detected quiet boundary.

Both complete Golden WAV hashes and source input hashes matched the locked manifest. `verify_golden.py` passed its context, checkpoint/official assets, runtime, stored output and quality checks. This was verification of existing reference artifacts, not a fresh RVC inference or ONNX/backend retest. No RVC implementation or assets changed.

The checkpoint metadata remains RVC v2, F0-enabled, 48 kHz, 768-dimensional features, 202 speaker embeddings, with the locked speaker ID 0. Reference crops were resampled once to 24 kHz and saved unchanged for reuse. [Reference provenance and hashes](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/qiqi-20261004/references.json), [Golden verification log](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/qiqi-20261004/golden-verification.log).

The same 13-word English test from the earlier benchmark was used: “Hello, this is Mozart, and this translation is spoken using a reference voice.” Chinese ZipVoice output used “你好，这里是莫扎特，现在用参考声音播报翻译后的文本。” Runtime was existing Sherpa 1.13.6 on CPU: Pocket two configured threads/five steps/seed 42; ZipVoice four threads/four steps/unseeded. `silence_scale=1` kept the full generated timeline. Model load is excluded from generation timings.

## Runtime results

| Engine/configuration | English, espeak-derived Qiqi reference | English, speech-derived Qiqi reference | Chinese, speech-derived reference | Peak process RSS |
|---|---:|---:|---:|---:|
| Pocket baseline | 4.09 s warm | **2.93 s warm** | English-only bundle; not tested | **0.623 GiB** |
| ZipVoice initial reference transcript | 10.59 s warm | 8.82 s warm | 6.08 s | 1.229 GiB |
| ZipVoice transcript from converted reference WAV | 12.48 s | 8.99 s | **6.19 s** | 1.325 GiB |

Pocket's first audio callback was 3.18 s for the espeak-derived reference and 2.36 s for the speech-derived reference. ZipVoice's single-sentence callback came at completion. Pocket generated 5.76/4.08 s outputs; ZipVoice initial English outputs were approximately 12.36/9.98 s. The follow-up Chinese output was 5.73 s.

All 13 generated outputs were finite and nonempty. Minimum board availability was **4.145 GiB** across these separate engine tests, with **zero measured swap increase** within each process. These were isolated Qiqi TTS runs, not a new Qiqi RVC + ASR + translation concurrency test.

The Chinese float outputs exceeded full scale slightly. The selected follow-up peaked at 1.054, with 0.00145% of samples beyond full scale; the initial espeak-reference Chinese output reached 1.374. Audition copies use explicitly recorded gain to leave 0.95 peak headroom before PCM16 conversion. Raw float WAVs and all original measurements are preserved; this playback gain does not repair text or voice fidelity.

## Text and reference-voice checks

The cached Whisper-small recognizer scored generated audio after TTS exited. Automatic transcripts are diagnostics, not human listening or an authoritative word transcript. Both source and converted reference ASR contained mistakes; this matters particularly for ZipVoice, which requires an accurate prompt transcript. The initial runs used source-audio ASR over the matching time span, and the follow-up used ASR from the exact converted-reference WAV. Neither prompt was manually confirmed.

| Test | English word error rate | CAMPPlus cosine to its Qiqi reference |
|---|---:|---:|
| Pocket, espeak-derived reference, warm | **0%** | **0.138** |
| Pocket, speech-derived reference, warm | 15.4% | **0.230** |
| ZipVoice initial, espeak-derived reference, warm | 84.6% | 0.356 |
| ZipVoice initial, speech-derived reference, warm | 23.1% | **0.486** |
| ZipVoice WAV-transcript follow-up, speech-derived reference | 38.5% | 0.410 |

Pocket recovered the full English test text with the espeak-derived reference, but speaker similarity was weak. In the speech-derived case, its score against the unrelated male control (0.291) exceeded its score against its own Qiqi reference (0.230). That is a useful warning, not a calibrated proof that the voice is wrong. The CAMPPlus model and preprocessing are the same exploratory checker described in the [first benchmark](/home/moyamryia/Projects/Project-Mozart/reports/zero-shot-tts-redesign-20261004/test-20261004/RESULTS.md). The two Qiqi reference crops themselves had cosine 0.562, reflecting their different source material/prosody.

ZipVoice scored closer to Qiqi, but English text accuracy varied substantially. Initial espeak-derived English generations had 30.8% and 84.6% WER; the WAV-transcript follow-up produced an unreliable output with WER over 100%, including recognizer insertions. Initial speech-derived English generations had 15.4% and 23.1% WER. Changing the transcript did not establish a reliable English fix. ZipVoice was unseeded, so transcript effects and generation variation cannot be cleanly separated from these few runs.

**Chinese was stronger with the speech-derived reference.** The initial output had 4.3% CER after normalizing the observed Traditional/Simplified character variants; the remaining difference was the name spelling 莫札特 versus 莫扎特. The WAV-transcript follow-up had **0% CER** and **0.669 cosine** against its Qiqi reference, versus 0.102 against the unrelated male control. The espeak-derived Chinese case was less intelligible, at 34.8% CER. This makes the speech-derived reference the better preview candidate among these available fixtures.

Traditional script spelling must not be counted as an acoustic failure: the scorer normalizes the observed variants 這裡聲參譯後報現 to 这里声参译后报现. Raw transcripts and the original unnormalized scoring file remain available. This limited mapping is not a general Traditional Chinese converter. [ASR results](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/qiqi-20261004/asr-evaluation.json), [identity results](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/qiqi-20261004/identity-evaluation.json).

## Decision and next step

1. Keep the direct text/reference architecture and engine adapter plan. The measured memory budget remains workable.
2. **Do not mark Qiqi English cloning ready** with these engines/reference clips. Pocket is fast and can speak the words, but retaining Qiqi's voice remains unresolved. ZipVoice's English word accuracy also remains unresolved.
3. Keep ZipVoice as a **Chinese preview candidate** using the speech-derived Qiqi clip. It still needs listening acceptance; a cosine score and correct transcript cannot establish naturalness or faithful character identity.
4. Use a clean original Qiqi recording with a confirmed transcript for the next quality test. A clone of an RVC-generated clip carries its source articulation and conversion artifacts. The current test does not establish that Pocket or ZipVoice will fail with original recordings.
5. Make profile readiness specific to **voice + engine + output language**, with a saved preview and acceptance status. The first human-reference benchmark must not automatically approve the Qiqi profile. If clean-reference English tests still fail, proceed to the previously planned higher-quality fallback benchmark rather than hiding the failure with waveform filters.

The verified trained Qiqi RVC path remains a useful identity reference for this comparison. No backend/API integration, RVC model switching, code commit or deployment was performed here.

## Listen side by side

- [Speech-derived Qiqi reference](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/qiqi-20261004/audition/reference-qiqi.wav).
- English: [Pocket](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/qiqi-20261004/audition/qiqi-pocket-english.wav), [initial ZipVoice](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/qiqi-20261004/audition/qiqi-zipvoice-initial-english.wav), [ZipVoice WAV-transcript follow-up](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/qiqi-20261004/audition/qiqi-zipvoice-english.wav).
- [Chinese ZipVoice preview](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/qiqi-20261004/audition/qiqi-zipvoice-chinese.wav).
- [Espeak-derived Qiqi reference](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/qiqi-20261004/audition/reference-qiqi-espeak.wav), [Pocket English with that reference](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/qiqi-20261004/audition/qiqi-pocket-espeak-english.wav).
- Original source spans before RVC: [espeak](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/qiqi-20261004/source-input-espeak-span.wav), [speech clip](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/qiqi-20261004/source-input-natural-source-span.wav).
- Full original PyTorch Golden files: [espeak Qiqi](/home/moyamryia/Projects/Project-Mozart/rvc-golden/output/qiqi-zh-espeak-pinyin-mixed-python-reference.wav), [speech-derived Qiqi](/home/moyamryia/Projects/Project-Mozart/rvc-golden/output/preprocessor-sample-mp4-30s-python-reference.wav).

[Benchmark code](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/qiqi-20261004/benchmark.py), [reference preparation](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/qiqi-20261004/prepare_references.py), [audition gains and hashes](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/qiqi-20261004/audition/manifest.json), [summary](/home/moyamryia/Projects/Project-Mozart/reports/zero-shot-tts-redesign-20261004/qiqi-20261004/summary.json).

The remote artifact directory is `/home/moyamryia/Mozart/rvc-golden/clone-tts/qiqi-20261004`. All 13 original generated WAV hashes, both reference crop hashes and all eight audition hashes were verified after copying them locally. The original Golden files and project Git history were preserved.
