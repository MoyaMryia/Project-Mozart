# RVC Golden reference

This directory gives the original PyTorch RVC reference for Mozart comparisons.
The reference does not use Mozart preprocessing or its C++ feature implementation.
It also does not use ONNX Runtime or TensorRT.
Reusable inputs, outputs, hashes, and captured tensors stay in this directory.
Dated project reports use [`reports/`](../reports/README.md).

## Comparison order

1. Make a deterministic input with espeak or espeak-ng.
2. Keep that exact WAV for the full investigation.
3. Run the original PyTorch path with official HuBERT/ContentVec and RMVPE assets.
4. Listen to the result before accepting the model as a reference.
5. Save the intermediate tensors.
6. Compare ONNX and PyTorch with same captured inputs.
7. Run the same input through the backend after those comparisons pass.
8. Replace one component at a time to find the first difference.

The minimum tensor set contains 16 kHz input, RMVPE mel/F0, HuBERT features, coarse pitch, continuous pitch, sequence length, speaker ID, and Generator output.
Comparisons include shape, dtype, layout, finite values, numerical error, cosine similarity, duration, and audio statistics.
The [agent instructions](../AGENTS.md) give the full procedure.

## Locked references

The manifest `golden_manifest.json`, version 4, stores the shared context and named standards.
The context records assets, runners, runtime, source commit, input, and parameters.
Each standard has a SHA-256 hash and WAV format fields.
Backend targets also specify `mode`, `model_id`, `corr_min`, and `f0_max_cents`.

| Standard | Recorded meaning |
| --- | --- |
| `offline-audible` | Original offline `generator.infer()` output with random VAE noise. |
| `offline-deterministic` | Offline output through the deterministic mean path. |
| `streaming-deterministic` | Mean-path output with legacy 2-second windows and Golden-aligned T398 framing. |
| `streaming-quality-audible` | Random-VAE quality reference with full-prefix computation and 2-second lookahead. |
| `streaming-quality-deterministic` | Mean-path output with the same quality framing. |

The recorded deterministic backend comparisons reached aligned correlation 1.0 and F0 difference 0.0 cents.
These results apply to the matching full-length and legacy streaming assets.
They do not specify the low-latency split Generator profile.
A deterministic export cannot reproduce an unrelated random-VAE realization byte for byte.

Run the manifest verifier:

```bash
/home/moyamryia/vc_backend_venv/bin/python rvc-golden/verify_golden.py
```

Reproduce the locked audible reference:

```bash
RVC_CUDA_GRAPH=0 /home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/verify_golden.py --reproduce offline-audible
```

The verifier returns exit code 1 for a context, hash, source cleanliness, or WAV format difference.
The `--model PATH` option selects a different checkpoint location.
The `--reproduce STANDARD` option also compares the generated WAV hash for equality.
The runner uses Torch and NumPy seed 114514.
Locked reproduction restores the original WAV `PEAK` timestamp to prevent metadata-only hash differences.

These absolute environment paths describe the recorded Jetson installation.
Equivalent assets and a compatible reference environment are necessary on a different host.

## MP4 references

The first 30 seconds of `preprocessor/sample.mp4` form a different locked input.
The manifest stores the source hash, FFmpeg arguments, extracted PCM16 input, model context, and output.

Reproduce the offline reference:

```bash
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/verify_golden.py \
  --reproduce preprocessor-sample-mp4-30s-offline-audible
```

Reproduce the quality streaming reference:

```bash
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/verify_golden.py \
  --reproduce preprocessor-sample-mp4-30s-streaming-quality-audible
```

The audition files are:

```text
output/preprocessor-sample-mp4-30s-python-reference.wav
output/preprocessor-sample-mp4-30s-streaming-quality-python-reference.wav
```

## Backend reproduction

Start the matching backend configuration:

```bash
build-gpu/state/mozart_stated rvc-golden/qiqi-zh-run/backend.yaml
```

In a different terminal, compare the backend targets:

```bash
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/verify_backend.py \
  --api http://127.0.0.1:18181 --udp 127.0.0.1:18101
```

The verifier drives HTTP file conversion and UDP streaming.
It compares correlation, F0, and RMS against the manifest limits.
It prints one PASS/FAIL result per standard and returns exit code 1 if any target fails.
Deterministic targets use the corresponding deterministic reference runners.

## Legacy and quality streaming

The legacy runner uses 20 ms input frames, a 2 s window, a 1.94 s hop, and a 60 ms crossfade.
This path gives a locked target for the matching legacy backend assets.
It does not define every current realtime configuration.

Quality mode computes each target from the full prefix and waits for 2 s of future context.
It creates an audition timeline without startup latency.
The time and memory necessary for this reference exceed those of the deployment path.
It also adds an 80-sample HuBERT guard and handles unvoiced RMVPE windows explicitly.
Both modes save exact window input, RMVPE intermediates, Generator inputs, and Generator output.

Reproduce the quality standards:

```bash
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/verify_golden.py --reproduce streaming-quality-audible
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/verify_golden.py --reproduce streaming-quality-deterministic
```

Compare a deterministic streaming audition with its offline reference:

```bash
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/compare_streaming_quality.py \
  rvc-golden/output/qiqi-zh-espeak-pinyin-mixed-python-reference-DET.wav \
  rvc-golden/output/qiqi-zh-espeak-pinyin-mixed-streaming-quality-python-reference-DET.wav \
  --stream-hop 31040
```

Raw stream files keep startup latency and final drain audio.
The `--audition-output` option keeps only the assembled content timeline.
Quality mode rejects prefixes beyond the original 41-second unsplit pipeline limit.
With a 2-second target and 2-second lookahead, its input limit is approximately 37 seconds.
Longer material must use different locked cases.

## Upstream realtime reference

The headless realtime runner uses a short block, past context, cached pitch, Generator head cropping, and SOLA alignment.
It gives a different reference for the upstream realtime path.

Run that reference:

```bash
RVC_CUDA_GRAPH=0 /home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/run_realtime_reference.py \
  --block-seconds 0.25 --extra-seconds 2.5 \
  --output /tmp/opencode/qiqi-upstream-realtime.wav
```

The runner defaults to CPU.
Its CPU time is not a backend latency estimate.
Realtime output can differ from the offline Golden output.
The matching upstream realtime output, objective measurements, and listening are necessary for its comparison.

The UDP client can exercise a backend profile:

```bash
/home/moyamryia/vc_backend_venv/bin/python \
  tools/stream_audio_udp.py \
  rvc-golden/input/qiqi-espeak-zh-en-mixed.wav \
  rvc-golden/output/qiqi-zh-espeak-zh-en-mixed-mozart-udp.wav \
  --backend 127.0.0.1:18000 \
  --api http://127.0.0.1:18080 \
  --model-id qiqi-zh-run
```

The command selects that model, sends 20 ms MZRT packets, and writes the 48 kHz replies.
The selected model determines the actual streaming profile.
UDP loss produces an error by default.
The diagnostic `--allow-missing` option permits incomplete replies.
The [deployment guide](../frontend/DEPLOYMENT.md#use-the-low-latency-profile) specifies the split realtime assets and selection procedure.

## Compare Generator exports

Before an asset replacement, compare the shipped model and candidate with same inputs:

```bash
python3 rvc-golden/compare_generator_models.py \
  --baseline rvc-backend/models/de_narrator/de_narrator.onnx \
  --candidate rvc-backend/models/de_narrator/generator_dynamic.onnx \
  --probe-lengths 50,100,200 \
  --output-dir rvc-golden/output
```

The script reads each model input contract.
It supplies same values from a fixed seed or captured tensors through `--tensors-dir rvc-golden/tensors`.
It reports absolute error, cosine similarity, RMS, and sample count.
The verdicts PASS, DEGRADED, and FAIL use exit codes 0, 2, and 1.
Length probes execute inference at each requested frame count.
A dynamic-axis claim must have satisfactory inference results at multiple lengths.

Lossless dtype conversions appear in the report.
Missing captured inputs, different output layouts, or failed candidate length probes prevent PASS.
Each model also runs twice with the same inputs.
Different repeated outputs indicate internal randomness and do not prove numerical equivalence.
A fixed input seed does not control random operators inside ONNX.
Deterministic exports or explicit shared noise are necessary for acceptance.

Run the comparison utility self-test without real models:

```bash
python3 rvc-golden/compare_generator_models.py --self-test
```

The dated [streaming investigation](STREAMING_BACKEND_INVESTIGATION.md) keeps the original evidence.
Its historical deployment claims must agree with the current code and assets.
