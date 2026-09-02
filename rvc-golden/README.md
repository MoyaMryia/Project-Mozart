# RVC Golden Reference

This directory is an isolated PyTorch reference for comparing the Mozart C++
RVC implementation against the original RVC inference path.

It intentionally does not use Mozart preprocessing, ONNX Runtime, TensorRT, or
the handwritten C++ mel/F0 implementation.

Run:

```bash
RVC_CUDA_GRAPH=0 /home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/run_reference.py
```

Inputs, outputs, model hashes, and intermediate tensors are kept under this
directory so each C++ stage can be compared numerically.

## Locked standards

`golden_manifest.json` (version 3) records a shared `context` (input, model,
runners, RVC source commit, HuBERT/RMVPE assets, parameters) and a list of
`standards`, each locked by SHA-256 + WAV format and independently verified:

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

Each standard carries a `repro` block (`mode`, `model_id`, `corr_min`,
`f0_max_cents`) used by the backend self-verifier. Verify the locked context
and every standard (hashes / formats only):

```bash
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/verify_golden.py
```

The verifier exits `1` if any SHA-256, RVC source commit, tree cleanliness, or
WAV format field differs. Use `--model PATH` when the model is elsewhere.

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

`run_streaming_reference.py` models the realtime data path without involving
the C++ backend. It consumes 20 ms / 320-sample frames, waits for a 32000
sample window, advances by 31040 input samples, and crossfades each 96000
sample generator result into a 93120-sample output block. Each block is saved
under the selected tensor directory, including the exact window, RMVPE
intermediates, Generator inputs, and Generator output.

Example for the checked-in qiqi-zh assets:

```bash
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/run_streaming_reference.py \
  --input rvc-golden/input/qiqi-espeak-zh-en-mixed.wav \
  --output rvc-golden/output/qiqi-zh-espeak-zh-en-mixed-streaming-python-reference.wav \
  --tensor-dir rvc-golden/tensors/qiqi_zh_zh_en_mixed_streaming
```

The output WAV intentionally contains the raw stream timeline, including the
two-second cold-start silence and the configured flush tail. The JSON beside
the WAV records the window and hop contract.

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
