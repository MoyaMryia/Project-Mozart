# RVC Golden Reference

This directory is an isolated PyTorch reference for comparing the Mozart C++
RVC implementation against the original RVC inference path.

It intentionally does not use Mozart preprocessing, ONNX Runtime, TensorRT, or
the handwritten C++ mel/F0 implementation.

Reproduce the locked qiqi audible reference through the original PyTorch RVC
path and require an exact WAV SHA-256 match:

```bash
RVC_CUDA_GRAPH=0 /home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/verify_golden.py --reproduce offline-audible
```

Inputs, outputs, model hashes, and intermediate tensors are kept under this
directory so each C++ stage can be compared numerically.

The runner fixes Torch/NumPy RNG with seed `114514`. During locked reproduction
it also restores the original WAV `PEAK` chunk timestamp recorded in the
manifest. Libsndfile otherwise writes the current time into that chunk, making
two sample-identical FLOAT WAV files have different whole-file hashes.

## Locked standards

`golden_manifest.json` (version 4) records a shared `context` (input, model,
runners, verifier, runtime, RVC source commit, HuBERT/RMVPE assets, parameters)
and a list of `standards`, each locked by SHA-256 + WAV format and independently
verified:

- `offline-audible` — the original RVC `generator.infer()` random-VAE offline
  reference (`output/...python-reference.wav`). A deterministic ONNX/TRT export
  cannot bit-match it (different RNG realization).
- `offline-deterministic` — the mean-path offline reference
  (`run_reference.py --deterministic-generator`, `...python-reference-DET.wav`).
  File-mode inference through the full-length `qiqi-zh-full` model reproduces
  it at aligned correlation 1.0 / F0 0.0 cents.
- `streaming-deterministic` — the mean-path 2 s-window streaming reference
  (`run_streaming_reference.py --deterministic-generator`,
  `...streaming-...-DET.wav`). The production streaming backend (golden-aligned
  T398 framing) reproduces it at aligned correlation 1.0 / F0 0.0 cents.
- `streaming-quality-audible` — the quality-first random-VAE streaming audition
  reference. It recomputes from the complete stream start with 2 s lookahead
  and restores the post-load RNG state for every prefix.
- `streaming-quality-deterministic` — the same quality-first framing with the
  deterministic mean-path Generator. This is the numerical quality ceiling for
  future C++ streaming work, not the current production framing contract.

Backend targets carry `mode`, `model_id`, `corr_min`, and `f0_max_cents` in
their `repro` blocks. Verify the locked context, hashes, formats, recorded
streaming quality metrics, and all-unvoiced F0 contract:

```bash
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/verify_golden.py
```

The verifier exits `1` if any SHA-256, RVC source commit, tree cleanliness, or
WAV format field differs. Use `--model PATH` when the model is elsewhere.
With `--reproduce STANDARD`, it additionally runs the appropriate reference
runner in a temporary directory and requires the generated WAV to match the
locked file byte for byte.

## Preprocessor MP4 reference

The first 30 seconds of `preprocessor/sample.mp4` are extracted as 16 kHz mono
PCM16 and run through the same offline qiqi PyTorch reference. The source MP4,
ffmpeg arguments, extracted input, model/code context, and output WAV are all
locked in the manifest.

```bash
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/verify_golden.py \
  --reproduce preprocessor-sample-mp4-30s-offline-audible
```

Audition output:
`output/preprocessor-sample-mp4-30s-python-reference.wav`.

The same extracted input has also passed the quality-first streaming path. Its
latency-free audition output is:
`output/preprocessor-sample-mp4-30s-streaming-quality-python-reference.wav`.

```bash
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/verify_golden.py \
  --reproduce preprocessor-sample-mp4-30s-streaming-quality-audible
```

To additionally reproduce each numeric standard against the **running backend**
(HTTP + UDP), which drives file mode and the streaming dataplane and checks
correlation / F0 / RMS against the manifest tolerances:

```bash
build-gpu/state/mozart_stated rvc-golden/qiqi-zh-run/backend.yaml &
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/verify_backend.py \
  --api http://127.0.0.1:18181 --udp 127.0.0.1:18101
```

`verify_backend.py` prints a per-standard PASS/FAIL line and exits `1` if any
standard is not reproduced within tolerance. Deterministic standards regenerate
via `run_reference.py --deterministic-generator --metadata <path>` and
`run_streaming_reference.py --deterministic-generator`.

## Streaming reference

`run_streaming_reference.py` has two purposes without involving the C++
backend:

- Its legacy defaults model the current realtime contract: 20 ms input frames,
  a 2 s window, a 1.94 s hop, and a 60 ms crossfade. This remains the production
  backend's locked numeric target.
- Its quality-first mode recomputes each 2 s target from the complete prefix,
  waits for 2 s of real future context, and emits a latency-free audition
  timeline. It deliberately spends much more time and memory to provide a
  stronger streaming debugging target. Deployment efficiency belongs in the
  C++ implementation, not this Golden ceiling.

Both modes save each window's exact input, RMVPE intermediates, Generator
inputs, and Generator output under the selected tensor directory. The
quality-first path also adds an 80-sample HuBERT guard, avoiding the legacy
20 ms zero-padding at each window end, and safely preserves all-unvoiced RMVPE
windows without exception-driven control flow.

Reproduce the checked-in quality-first qiqi standards:

```bash
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/verify_golden.py --reproduce streaming-quality-audible
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/verify_golden.py --reproduce streaming-quality-deterministic
```

Compare a streaming audition against its offline deterministic Golden:

```bash
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/compare_streaming_quality.py \
  rvc-golden/output/qiqi-zh-espeak-pinyin-mixed-python-reference-DET.wav \
  rvc-golden/output/qiqi-zh-espeak-pinyin-mixed-streaming-quality-python-reference-DET.wav \
  --stream-hop 31040
```

Raw stream outputs retain startup latency and flush. Files passed through
`--audition-output` contain only the assembled content timeline, cropped to the
same HuBERT frame contract as the corresponding offline result.
Quality-first mode rejects any prefix whose target plus lookahead can exceed the
original RVC pipeline's 41-second unsplit limit. With the locked 2 s target and
2 s lookahead, inputs are therefore limited to about 37 seconds. Split longer
test material into locked cases rather than silently changing the output-length
rule.

The realtime UDP client is separate from this Golden runner because the
backend's audio data plane is UDP, not HTTP:

```bash
/home/moyamryia/vc_backend_venv/bin/python \
  tools/stream_audio_udp.py \
  rvc-golden/input/qiqi-espeak-zh-en-mixed.wav \
  rvc-golden/output/qiqi-zh-espeak-zh-en-mixed-mozart-udp.wav \
  --backend 127.0.0.1:18000 \
  --api http://127.0.0.1:18080 \
  --model-id qiqi-zh-run
```

That command activates `rt_rvc`, sends 20 ms `MZRT` packets, drains the
two-second realtime window with silence, and writes the 48 kHz replies. UDP
loss is an error by default; `--allow-missing` is available for diagnostics.
