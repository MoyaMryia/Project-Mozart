# GitHub alternatives for translated voice cloning — 2026-10-04

Goal: speak translated text in a reference voice on Jetson Orin Nano 8GB. This is a source/documentation review, not a deployment benchmark. The user did not recall one specific repository, so this compares likely alternatives by inference path and integration cost.

Subsequent work: [actual Jetson tests](/home/moyamryia/Projects/Project-Mozart/reports/zero-shot-tts-redesign-20261004/test-20261004/RESULTS.md) now cover ZipVoice and Pocket, including ASR + Qwen + Pocket concurrency. Pocket is faster and smaller; ZipVoice scored higher on the exploratory speaker similarity check. This review's untested status below describes when the repository comparison was written.

## Shortlist

| Repository | What it supplies | Assessment for Mozart |
|---|---|---|
| [k2-fsa/ZipVoice](https://github.com/k2-fsa/ZipVoice) via [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | Chinese/English reference-conditioned TTS; distilled ONNX release and native APIs | Keep as the first bilingual baseline. Existing Sherpa 1.13.6 exposes the API. Needs matching reference transcript and measured CPU performance/whole-board memory. |
| [kyutai-labs/pocket-tts](https://github.com/kyutai-labs/pocket-tts) via Sherpa | Small CPU-oriented reference-conditioned TTS; released English ONNX package | Keep as the English-output challenger. Chinese reference → English identity and the exact pinned ONNX bundle require validation; current upstream multilingual releases do not establish compatibility for that English bundle. |
| [myshell-ai/OpenVoice](https://github.com/myshell-ai/OpenVoice) V2 | Base TTS followed by reference-conditioned tone conversion; official demo uses MeloTTS | Add as a practical third benchmark. Mozart already has a Melo ONNX model, but matching its source speaker embedding/sample rate to the official converter is not established. This is two inference stages; measure both and their combined memory. |
| [predict-woo/qwen3-tts.cpp](https://github.com/predict-woo/qwen3-tts.cpp) | C++17/GGML runtime for Qwen3-TTS, including cloning, GGUF F16/Q8 and CUDA backend support | Strong alternative runtime for the Qwen 0.6B Base fallback. It avoids Python at inference, but does not prove smaller memory. Its README reports clone speedup 4.07× and peak RSS **+7.7%** relative to its PyTorch baseline; those numbers are not Nano results. |
| [0xShug0/audio.cpp](https://github.com/0xShug0/audio.cpp) | A broader GGML/C++ runtime supporting PocketTTS, Qwen3-TTS and other audio models | Worth comparing if native Sherpa falls short. It is a runtime alternative, not inherently a smaller model. Evaluate only the required TTS family; do not replace Mozart's verified IO/ASR/RVC just because the repository also supports them. Quantization support/stability is model-specific. |

Support references: [Sherpa ZipVoice](https://k2-fsa.github.io/sherpa/onnx/tts/zipvoice.html), [Sherpa PocketTTS](https://k2-fsa.github.io/sherpa/onnx/tts/pocket.html), [OpenVoice usage](https://github.com/myshell-ai/OpenVoice/blob/main/docs/USAGE.md), [OpenVoice V2 demo](https://github.com/myshell-ai/OpenVoice/blob/main/demo_part3.ipynb).

## Other plausible remembered alternatives

| Repository | Relevance | Priority |
|---|---|---|
| [QwenAudio/CosyVoice](https://github.com/QwenAudio/CosyVoice), formerly FunAudioLLM/CosyVoice | Direct multilingual/cross-language zero-shot TTS. Current repository includes 0.5B CosyVoice 2/3 checkpoints and TensorRT-LLM deployment material. | Quality fallback. No Nano memory/latency proof was found in the inspected material; a different 0.5B stack is not automatically lightweight. |
| [SWivid/F5-TTS](https://github.com/SWivid/F5-TTS) | Reference-conditioned flow-matching TTS; official README links community ONNX and TensorRT deployment projects. | Quality/export fallback. Validate the complete text/conditioning/sampling/vocoder path; an available export does not establish board compatibility or low memory. |
| [resemble-ai/chatterbox](https://github.com/resemble-ai/chatterbox) | Reference-conditioned TTS. Turbo is listed as a 350M English model with lower compute/VRAM than the other listed Chatterbox variants. | English-output fallback if the smaller shortlist fails quality. Its relative efficiency claim is not a Mozart combined-memory measurement. |
| [coqui-ai/TTS](https://github.com/coqui-ai/TTS), XTTS-v2 | Direct multilingual cloned TTS; [official weights](https://huggingface.co/coqui/XTTS-v2) list 17 languages and the Coqui Public Model License. | Later fallback; more runtime/licensing integration to pin. No Nano footprint was verified here. |
| [bshall/knn-vc](https://github.com/bshall/knn-vc) | Audio-to-audio conversion using WavLM, nearest-neighbor matching and HiFi-GAN. | Separate VC alternative. It still needs base TTS for translated text, and simpler matching does not establish a smaller total footprint. |
| [nathanael-perraudin-synth/qwen3-tts-cuda-graphs](https://github.com/nathanael-perraudin-synth/qwen3-tts-cuda-graphs) | Qwen runtime optimizations using CUDA graphs and cached speaker conditioning. | Useful optimization reference. Published Jetson table includes **AGX Orin 64GB**, not our Nano 8GB. Graph capture can trade memory for speed; do not transfer the AGX results to this board. |

## OpenVoice-specific integration findings

The [official extractor](https://github.com/myshell-ai/OpenVoice/blob/main/openvoice/se_extractor.py) has a VAD path and a transcription-based path that lazily loads Whisper **medium** on CUDA. A benchmark must identify which path is used and include preprocessing dependencies in its memory trace. Clean, manually segmented reference fixtures can be passed to the converter's extraction API so an unrelated transcription model does not define the steady synthesis footprint.

The [converter API](https://github.com/myshell-ai/OpenVoice/blob/main/openvoice/api.py) supports storing extracted reference embeddings. Cache validated target embeddings and the matching source-speaker embedding once per engine/profile revision. The official V2 demo loads Melo's source-speaker embeddings, synthesizes base audio, then performs conversion. Reusing Mozart's existing Melo export requires proof that the voice ID, source embedding and base waveform correspond to that path; do not silently substitute arbitrary Matcha/Melo voices.

Use the same SpeechEngine/Voice Registry/job interface from the redesign plan for this two-stage adapter. The application should not need another global mode just to switch between direct cloned TTS and base-TTS-plus-conversion.

## Decision and next test

1. Benchmark **ZipVoice-Distill int8 and PocketTTS int8 through the installed Sherpa API** on identical Chinese-reference/English-output fixtures.
2. Add **OpenVoice V2 + matching Melo** as the third comparison. This directly tests whether reusing a base synthesizer is preferable to a new direct cloning engine.
3. If quality is insufficient, test **Qwen3-TTS 0.6B Base with a native runtime**. Compare the upstream PyTorch reference first; verify the actual cloning mode, sampled/greedy settings and retained identity. A C++ implementation can improve speed while increasing memory.
4. Keep CosyVoice/F5/Chatterbox/XTTS as contingent candidates, rather than loading every model or building every stack before the first result.

Record engine/load/prompt/synthesis timings, total and incremental whole-board memory, RSS/PSS, swap change, CPU contention, paired audio and target-text accuracy. Use the same reference, prompt text and translations across engines. Measure with Qwen translation and ASR active after isolated correctness passes, then optionally add realtime RVC.

The earlier **0.8–1.5 GiB cloning allowance is a budget hypothesis**, not a verified prediction for every candidate or runtime. No lower memory estimate should replace it just because a repository says C++, ONNX, int8 or “realtime.” The selection still requires at least 1 GiB available RAM and no sustained swap growth in the intended workload.

No model was downloaded, installed, built or run during this repository comparison. Existing Jetson assets, environments and unpushed documentation commits were preserved.
