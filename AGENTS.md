# Agent Instructions

## RVC Debugging Workflow

When investigating RVC quality, correctness, or export regressions, use the
following order. Do not begin by changing the C++ pipeline based only on
subjective output.

1. Create a clean, deterministic test input with `espeak` or `espeak-ng`.
   Use an appropriate language and voice for the model being tested. Keep the
   generated WAV and reuse the exact same file throughout the investigation.

2. Run the input through a known-good Golden Model first.
   Use the original PyTorch RVC implementation with official HuBERT/ContentVec
   and RMVPE assets. Confirm that the checkpoint, language, and input produce a
   correct audible result before treating that model as a reference.

3. Capture the Golden Model's intermediate tensors.
   At minimum, save the 16 kHz input, RMVPE mel and F0, HuBERT features, coarse
   pitch, continuous pitch, sequence length, speaker ID, and Generator output.

4. Compare the exported ONNX models against PyTorch before backend testing.
   Feed identical captured tensors to both implementations and compare shapes,
   dtypes, finite values, numerical error, cosine similarity, output duration,
   and basic audio statistics. An ONNX file loading successfully does not prove
   that its weights or outputs are correct.

5. Only after the Golden and ONNX paths pass, run the exact same input through
   the Mozart backend API. Keep pitch shift, index rate, RMS mix, protect, and
   preprocessing settings fixed between comparisons.

6. Bisect the pipeline by swapping one component at a time:
   - Golden tensors -> ONNX Generator
   - Python RMVPE -> C++/ONNX Generator path
   - C++ RMVPE -> Golden HuBERT and Generator path
   - Python HuBERT -> backend Generator path
   - C++ HuBERT -> Golden Generator path
   - Backend preprocessing and chunking -> otherwise verified inference path

7. Fix the first stage that diverges, then repeat the same comparison before
   moving downstream. Do not compensate for an upstream tensor mismatch with
   waveform filters or parameter tuning.

## Required Checks

- Verify checkpoint metadata: RVC version, F0 support, sample rate, speaker
  count, and feature dimension.
- Match RVC v2 with final-layer 768-dimensional HuBERT features. Do not use the
  RVC v1 layer-9 projection for v2 models.
- Verify tensor memory layout even when dimensions are equal. RMVPE input is
  `[batch, mel_bin, time]`; a 128x128 shape cannot reveal a transposition bug.
- Load checkpoint weights before removing weight normalization. Treat missing
  or unexpected Generator weights as an export failure.
- Do not advertise dynamic ONNX axes unless multiple sequence lengths have
  actually passed inference. Mozart's current Generator contract is fixed at
  `T=200`.
- Confirm which runtime asset is loaded. A neighboring TensorRT `.engine` takes
  precedence over its `.onnx` file.
- For clean file tests, avoid realtime RNNoise/VAD processing unless that stage
  is specifically under test.
- Compare audible output plus objective measurements such as RMS, spectral
  centroid, F0 agreement, duration, clipping, and discontinuities at chunk
  boundaries.

## Test Artifacts

Keep reusable reference inputs, outputs, assets, and captured tensors under
`rvc-golden/`. Name outputs so the execution path is explicit, for example:

- `model-espeak-python-reference.wav`
- `model-espeak-onnx-generator.wav`
- `model-espeak-mozart-backend.wav`

When reporting results, provide the original input, Golden output, ONNX output
when relevant, and backend output so they can be auditioned side by side.
