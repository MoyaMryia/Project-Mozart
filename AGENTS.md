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
- State the claim before the limit. Write each limit once.
- Put the scope in one section at the end of the document. List the configuration, the sample count, and the unverified items there.
- Do not repeat a limit in more than one document. Link to the document that owns the limit.
- Treat a hedge sentence as an open task. Resolve it, or record the blocker and the release condition.

### 中文

中文采用简明技术表达。参考 GB/T 19678.1—2018 的使用说明编制要求、
GB/T 15834—2011 的标点规则，以及 GB/T 1.1—2020 中适用的结构和表述规则。
这些规范用途不同，不称为“中文版 ASD-STE100”，也不声称存在对应的通用受控词表。

- 使用短句。一句说明一件事；一句操作只包含一个主要动作。
- 写明主体、条件、动作和结果，避免含糊指代。
- 同一概念使用同一术语。首次出现缩写或必要英文术语时说明含义。
- 先给结论，再给必要证据。删除重复、宣传措辞和没有依据的保证。
- 明确区分实测结果、代码确认、推断和未验证项。每项在文档中只标注一次。
- 每份实测报告在文末设“适用范围”一节，列出配置、样本数和未验证项。
- 先写主张，再写限制。限制最多一句。
- 只在读者可能作出错误推论时写限制。不要在句尾追加“不是……”“不能……”“不代表……”。
- 同一条限制只在一个文档中说明。其他文档使用链接。
- 不预判审稿人。删除“不能通过……宣称……”这类针对假设反对意见的句子。
- 性能数字注明配置、计时起止点和统计范围；完成率注明分母。
- 中文正文使用中文标点；数值与单位之间留空格，例如 `320 ms`、`8 GB`。

### Accuracy and Source Text

技术准确性优先。命令、路径、标识符、接口字段、日志、引用和人工确认原文保持准确。
不为满足语言规则修改其含义。历史证据保留原文，并标明日期和适用范围。
句长检查不能替代词义、词性和技术审查；未经完整审核，不宣称全文符合性或认证。

以上规则只删除重复和否定的句子。实测值、配置和未验证项必须保留。

不合格示例：`该指标为 0.92。它不代表 92% 的句子翻译正确，也不是实测的重叠率。该实验不能证明时长约束能保留全部事实。`
合格示例：`该指标为 0.92，定义为 1 - |参考时长 - 预测时长| / 参考时长。它使用预测帧数。适用范围见文末。`

## 未解决项与限制

写限制句之前先判断：一次测试、一次测量或一次代码修改能否解决该问题。
可以解决时，继续做。不要用限制句结束该问题。
只有缺少硬件、权限或时间时，才把它记为未验证项。同时写明阻塞原因和解除条件。
“不是……”“不能保证……”“仍需验证……”表示工作没有完成。它们是待办，不是结论。
未解决的项在“适用范围”一节集中列出。不要分散重复。

## 代码校验与降级

在入口校验外部输入一次。外部输入包括 HTTP 请求、命令行参数、配置文件和其他进程的输出。
内部函数信任已经校验的值。内部函数不重复校验。
同一个限制只定义一处。其他位置引用该定义。
调用方已经夹住参数时，不写不会被触发的守卫。删除守卫，或删除夹取。
守卫必须在它声称要拦住的错误出现时失败。构造该错误后仍然放行的守卫，删除或改写。
不要保留只有测试能触发的校验分支。
校验失败时返回明确错误并停止。不要捕获内部错误后继续运行。
降级时记录原因，并让状态接口可以查询该原因。
不要构造对象只为了触发校验。
`except` 只捕获当前能够处理的异常。
不要为仓库内的资源做启动时全量校验，除非已记录对应的故障。

### 本仓库的不合格示例

重复校验：`TempoController` 已把比例夹在 `[1, MAX_TEMPO]`。
`TempoPlayer.__init__` 又检查一次，该分支只有测试能触发。
合格做法：只在 `TempoController` 和命令行入口校验。删除 `TempoPlayer` 中的检查及其测试。

同一限制定义多处：`tools/tempo_control.py` 定义 `MAX_TEMPO = 1.15`。
`preprocessor/src/tempo_filter.c` 又硬编码两处 `1.15`。
合格做法：C 侧使用一个宏。增加一个测试，比较 Python 和 C 的上限。

宽泛捕获：`tools/asr_checks.py` 用 `except Exception` 把所有错误记为 `numeric_check_failed`。
`numeric_audit` 自身的程序错误也会变成质量标记，然后程序继续运行。
合格做法：只捕获识别调用可能抛出的异常类型。其他异常向上抛出。

放宽验收：`verify_tempo_playback.py` 把 `exact` 改为与同一原生过滤器比较。
与 FFmpeg 参考的差异不再导致失败。
合格做法：保留原有失败条件。需要容差时，写明容差数值和依据。

`tools/writing_lint.py` 检查宽泛 `except` 和限制句。基线只能缩小，规则见脚本说明。

### 交付前的子代理审查

修改代码后、交付前，开一个 `reviewer` 子代理审查本次 diff。
给子代理以下检查项，要求逐项列出文件和行号：

1. 内部函数重复校验调用方已经校验或夹取的值。
2. 只有测试能触发的守卫或校验分支。
3. 同一个限制、常量或阈值在多处定义。
4. 宽泛 `except`，或捕获后继续运行而不记录可查询原因。
5. 验收条件被放宽、改为自我比较，或失败被改成记录字段。
   对每个守卫和验收条件，说明构造它要拦住的错误后，它是否失败。
6. 文档或注释中的限制句，以及没有阻塞原因和解除条件的未验证项。

子代理只报告问题，不修改文件。
主代理逐项修复，或在回复中写明保留的理由。
不要只靠子代理结论交付。修复后重新运行测试和 `tools/writing_lint.py`。

### 提交前检查

提交前运行 `tools/ci-local.sh --staged`。pre-commit hook 调用同一命令。
首次使用时运行 `tools/install-hooks.sh` 安装 hook。
不要用 `--no-verify` 跳过 hook。
lint 检查暂存区内容，即将要提交的内容。
暂存的文件触发对应阶段。触发路径定义在 `tools/ci-local.sh`。
检查范围内有未暂存或未跟踪的改动时，hook 失败。暂存、还原或移走这些改动后再提交。
CI 调用同一脚本的各阶段。检查命令只在 `tools/ci-local.sh` 中定义。

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
