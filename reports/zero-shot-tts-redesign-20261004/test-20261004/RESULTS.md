# Reference-conditioned TTS on Jetson — measured results, 2026-10-04

**Direct cloned TTS fits this Jetson. PocketTTS is the fastest English-output candidate; ZipVoice has stronger measured reference-speaker similarity. Neither has passed a human listening acceptance test.** Proceed with the text/reference TTS architecture, using Pocket for the performance prototype and retaining ZipVoice for the voice-quality comparison.

## Test conditions

- Jetson Orin Nano Super, 7,546 MiB visible RAM, existing MAXN_SUPER mode and schedutil governor. No clocks or power settings changed.
- Mozart source `fd9a0fc55bc6b84baa16f5d7eb91c0a02eb5cd5b`; its two unpushed documentation commits were preserved.
- Installed `sherpa-onnx 1.13.6`, **CPU provider**. Baseline: two configured inference threads; ZipVoice optimization: four. Existing Python environments were reused without installing or changing dependencies.
- ZipVoice-Distill int8 Chinese/English + Vocos 24 kHz; PocketTTS int8 bundle dated 2026-01-26. Official GitHub release SHA256 values matched locally and on the Jetson. See [asset manifest](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/test-20261004/asset-manifest.json).
- Identical packaged Chinese male (6.057 s) and female (8.836 s) references, with their matching published transcripts for ZipVoice. An English `espeak-ng` reference was generated once and reused as a synthetic control. These are public test fixtures, not the user's voice.
- Shared English test: “Hello, this is Mozart, and this translation is spoken using a reference voice.” Pocket uses five sampling steps and seed 42; ZipVoice uses four steps unless explicitly marked. ZipVoice generation was not seeded, so output variation limits strict quality comparisons between configurations.
- No RNNoise/VAD, microphone capture, physical playback, RVC or Seed-VC in these tests. This is a native Sherpa ONNX benchmark, not a PyTorch/export parity investigation or an RVC correctness claim.

## Synthesis measurements

Generation time includes reference conditioning and audio generation, but excludes loading the engine. RSS includes the runtime and all components loaded in that process. RTF is generation time divided by final output duration. Callback latency means samples reached the application; it is not physical speaker latency.

| Configuration | Model load | Male-reference English | Female-reference English | Peak process RSS |
|---|---:|---:|---:|---:|
| Pocket, 2 threads, 5 steps | 1.10 s | **3.37 s warm**, first callback **2.67 s** | 3.34 s, first callback 2.70 s | **0.613 GiB** |
| ZipVoice, 2 threads, 4 steps | 3.48 s | 11.97 s warm | 15.10 s | 1.086 GiB |
| ZipVoice, 4 threads, 4 steps | 3.28 s | 7.45 s | 9.36 s | 1.088 GiB |
| ZipVoice, 4 threads, 2 steps | 3.25 s | 4.06 s | 4.98 s | 1.084 GiB |

Only the baseline male-reference cases have repeated warm runs. Optimized configurations have one generation per reference after model loading. Pocket's first baseline generation took 3.58 s; subsequent male runs took 3.371 and 3.376 s. Its output duration was 4.72 s, giving warm RTF approximately 0.715. Baseline ZipVoice male outputs were 7.85–8.83 s; female output was 6.03 s. No isolated configuration increased swap.

ZipVoice also generated Chinese text in 5.01 s using four threads/four steps, with a 5.21 s output. It had a small overload: peak 1.170 and 0.0024% of samples at or beyond full scale. The original float WAV is retained; this needs output gain/headroom handling before integer playback. English test outputs were finite, nonempty and unclipped.

## Text and identity checks

After synthesis processes exited, the already cached Whisper-small model transcribed selected outputs, with an explicit language/decoder prefix and attention mask. The evaluator is separate from the TTS resource measurements. WER below is a small-fixture diagnostic, not a general benchmark or a human intelligibility score.

| Configuration | Male English WER | Female English WER | CAMPPlus cosine to correct male / female reference |
|---|---:|---:|---:|
| Pocket baseline | 7.7% | **0%** | 0.328 / 0.241 |
| ZipVoice 2 threads, 4 steps | 15.4% | 23.1% | 0.545 / 0.423 |
| ZipVoice 4 threads, 4 steps | **0%** | 15.4% | **0.573 / 0.432** |
| ZipVoice 4 threads, 2 steps | 23.1% | **53.8%** | 0.475 / 0.253 |

Whisper omitted “Hello” from Pocket's male output. The English synthetic-reference controls for both baseline engines transcribed exactly. ZipVoice's Chinese output had 0% character error on this sentence. Reducing ZipVoice to two steps helped latency but worsened the tested word accuracy; do not select that configuration solely for speed.

CAMPPlus used the existing checkpoint and the same pure-Torch 80-bin filterbank and mean subtraction used by the archived Seed-VC inference code. Each generated clip was compared with both references and the synthetic control. The correct reference scored higher than the alternate Chinese reference, but **there is no calibrated acceptance threshold**, and cross-language embeddings have limitations. ZipVoice's stronger scores make it important to retain for listening comparisons rather than choosing Pocket on speed alone. The Chinese same-language ZipVoice clip scored 0.679 against its male reference.

The existing TorchAudio package cannot import because its CUDA build is 13.0 while Torch is 13.2. For this auxiliary metric only, the installed pure-Torch filterbank source was executed with its unused `import torchaudio` omitted; no MFCC code was called and no installed files were changed. Source/checkpoint hashes and the preprocessing are in [identity results](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/test-20261004/identity-evaluation.json). This dependency mismatch is not a Pocket/ZipVoice runtime requirement.

## ASR + translation + Pocket together

The bounded run used the current Chinese Zipformer 14M recognizer on CPU, Qwen3.5-0.8B Q4_K_M through the existing CUDA llama-server, and Pocket on CPU. Qwen used context 2,048, one server slot and two CPU threads, binding only a temporary loopback port. ASR fed the same reference recording in 20 ms chunks paced to realtime while translation requests and TTS ran. RVC stayed unloaded.

- Run duration: approximately **59 seconds**. Completed **15 TTS jobs, 8 full ASR passes and 32 translation requests**; no recorded worker errors. One interrupted ASR pass at cleanup is explicitly marked and excluded from the completed count.
- **Minimum available RAM: 4.197 GiB.** Maximum unavailable RAM (`MemTotal - MemAvailable`) was approximately 3.17 GiB; this is a whole-board pressure measure, not the sum of model RSS or `free`'s used column.
- TTS + ASR process peak RSS: **0.687 GiB**; separate Qwen server peak RSS: **1.284 GiB**. Unified GPU memory is included in the board's availability reading; it must not be added again as separate VRAM.
- Pre-existing swap was about 637.5 MiB. Maximum increase was **0.25 MiB**, not literally zero. This short run showed ample headroom and no progressive large swap increase; it does not establish prolonged stability.
- Median translation request: **0.624 s**. Median ASR decode work per 6.057 s clip: **0.541 s**. These are component measurements, not microphone-to-speaker latency.
- Standard male-reference TTS: 3.03–3.38 s, first callback 2.33–2.69 s. Female-reference TTS: 2.81–3.10 s, first callback 2.18–2.47 s.
- Actual longer translated-source TTS: **3.90–4.01 s** for 6.08 s of English audio, first callback **2.93–3.04 s**.

Recognized input: `那还是三十六年前一九八七年我考上了武汉大学的计算机系`

Translation: “Then it was still thirty-six years ago, in 1987, when I enrolled in the Computer Science department at Wuhan University.”

Whisper transcribed the synthesized translation as “And it was still 36 years ago in 1987 when I enrolled in the computer science department at Wuhan University.” Raw WER was 14.3%, including number-format differences and the changed opening word. The year and university were retained. Human listening is still needed.

The server was terminated by the test's cleanup. No project service configuration, backend code or Git commits changed. Simultaneous RVC, preprocessing, UI traffic, physical audio routing, cancellation and queue pressure remain separate integration tests.

## Playback implications and next implementation

1. **Use Pocket as the English performance prototype**, with one warm engine, bounded voice conditioning cache and two configured CPU threads. Keep ZipVoice four-step as the bilingual/identity comparator. These measurements support the earlier small-TTS memory budget; they do not establish a footprint for Qwen-TTS, OpenVoice or every alternative.
2. Add the planned voice profile/preview and text speech job seam. Validate with the user's reference recordings and several translations before finalizing the engine. Prioritize dropped/opening words and cross-language identity; do not declare an identity pass from one embedding score.
3. Start with complete-sentence WAV playback. Pocket callbacks in this implementation begin after sentence token generation; ZipVoice delivered one callback at completion for these single-sentence cases. Their existence does not imply frame-by-frame low-latency synthesis.
4. Keep Pocket `silence_scale=1` for a future callback path: baseline 0.2 postprocesses the completed WAV's silence, so concatenated callback samples and the final WAV can have different lengths. The follow-up increased female output from 3.587 to 4.400 s and preserved the same zero-WER transcript. This setting did not restore the missing male “Hello.”
5. Run a longer queue/playback soak and then optional RVC concurrency using the verified RVC fixtures. No current result proves that every project feature can be active simultaneously.

The pinned Pocket bundle's LICENSE says CC-BY-4.0, while its README says non-commercial. Copies of both are retained with the artifacts; resolve that inconsistency for any distribution decision rather than substituting current upstream terms for this exact bundle.

## Audition and reproduce

Audition copies are PCM16 conversions of the unchanged English float outputs, for broad player compatibility. The original float outputs and their hashes remain in each run directory.

- [Chinese male input/reference](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/test-20261004/audition/reference-zh-male.wav)
- [Actual translated-source Pocket output](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/test-20261004/audition/pocket-translated-source.wav)
- Same standard English text: [Pocket male](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/test-20261004/audition/pocket-standard-male.wav), [ZipVoice male](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/test-20261004/audition/zipvoice-standard-male.wav).
- [Chinese female reference](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/test-20261004/reference-zh-female.wav), [Pocket female](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/test-20261004/audition/pocket-standard-female.wav), [ZipVoice female](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/test-20261004/audition/zipvoice-standard-female.wav).
- [Deterministic English control](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/test-20261004/reference-en-espeak.wav), [Pocket control output](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/test-20261004/pocket/pocket-en_espeak_control.wav), [ZipVoice control output](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/test-20261004/zipvoice/zipvoice-en_espeak_control.wav).

[Benchmark source](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/test-20261004/benchmark.py), [concurrent driver](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/test-20261004/concurrent_benchmark.py), [ASR evaluation](/home/moyamryia/Projects/Project-Mozart/rvc-golden/clone-tts/test-20261004/asr-evaluation.json), [numeric summary](/home/moyamryia/Projects/Project-Mozart/reports/zero-shot-tts-redesign-20261004/test-20261004/summary.json).

On the Jetson, the artifacts are `/home/moyamryia/Mozart/rvc-golden/clone-tts/test-20261004` and assets are `/home/moyamryia/models/sherpa-onnx/clone-test-downloads`. The benchmark's memory guard stops below 768 MiB available. For example, from the artifact directory:

```sh
/home/moyamryia/Mozart/.venv/bin/python benchmark.py --engine pocket \
  --assets /home/moyamryia/models/sherpa-onnx/clone-test-downloads \
  --fixtures fixtures.json --output pocket-recheck --threads 2 --silence-scale 1
```

The evaluation scripts use the existing `/home/moyamryia/vc_backend_venv/bin/python` and cached Whisper/CAMPPlus weights. The production TTS benchmark uses only the existing Mozart Sherpa environment. All 32 current test WAVs were copied back and checked against their recorded output hashes.
