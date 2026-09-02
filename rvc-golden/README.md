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

## Locked standard

The checked-in standard is
`output/qiqi-zh-espeak-pinyin-mixed-python-reference.wav`. Its exact WAV
bytes, source input, external `.pth` model, both runners, RVC source commit,
and HuBERT/RMVPE assets are recorded in `golden_manifest.json`. Verify the
complete context before using a result as a regression reference:

```bash
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/verify_golden.py
```

The verifier exits with status `1` if any SHA-256, RVC source commit, source
tree cleanliness, or WAV format field differs.
Use `--model PATH` when the model is stored at a different location; the
expected model hash remains the one recorded in the manifest.

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
