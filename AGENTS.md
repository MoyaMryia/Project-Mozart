# Agent Instructions

## Language and Communication

以下规则适用于用户对话、进度更新、最终答复、文档、提交说明和新增代码注释。
默认使用用户当前使用的语言。完整规则与项目术语见 [docs/WRITING.md](docs/WRITING.md)。

### English

- Obey the writing rules and controlled dictionary of ASD-STE100 Issue 9.
- Write instructions in the imperative form. Give one instruction per sentence. Use a maximum of 20 words per sentence.
- Use a maximum of 25 words per descriptive sentence. Keep each paragraph to one topic and a maximum of six sentences.
- Use active voice, clear subjects, and consistent terms. Use each word with its approved meaning and part of speech.
- Use project technical nouns and technical verbs as specified in the glossary. Do not add exceptions without a technical reason.
- Give conditions before instructions. Do not use figurative language, contractions, or unclear pronouns.

### 中文

中文采用简明技术表达。参考 GB/T 19678.1—2018 的使用说明编制要求、
GB/T 15834—2011 的标点规则，以及 GB/T 1.1—2020 中适用的结构和表述规则。
这些规范用途不同，不称为“中文版 ASD-STE100”，也不声称存在对应的通用受控词表。

- 使用短句。一句说明一件事；一句操作只包含一个主要动作。
- 写明主体、条件、动作和结果，避免含糊指代。
- 同一概念使用同一术语。首次出现缩写或必要英文术语时说明含义。
- 先给结论，再给必要证据。删除重复、宣传措辞和没有依据的保证。
- 明确区分实测结果、代码确认、推断和未验证项。
- 性能数字注明配置、计时起止点和统计范围；完成率注明分母。
- 中文正文使用中文标点；数值与单位之间留空格，例如 `320 ms`、`8 GB`。

### Accuracy and Source Text

技术准确性优先。命令、路径、标识符、接口字段、日志、引用和人工确认原文保持准确。
不为满足语言规则修改其含义。历史证据保留原文，并标明日期和适用范围。
句长检查不能替代词义、词性和技术审查；未经完整审核，不宣称全文符合性或认证。

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
