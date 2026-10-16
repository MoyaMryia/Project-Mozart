# Codex 改动审查

审查日期：2026-10-16，UTC+8。
审查对象：提交 `fc774b6` 至 `c76e990`，共 18 个提交，以及工作区未提交改动。
方法：4 个 `reviewer` 子代理按 AGENTS.md“交付前的子代理审查”清单分区审查。主代理按行号复核了标为“已复核”的条目。
结论：共 20 项。高优先级 3 项，中优先级 9 项，低优先级 8 项。高优先级问题集中在验收门控和默认值依据。

## 修复顺序

1. 先修门控：H1–H3、M1–M5。门控失效时，后续修复的验证结果没有依据。
2. 再修重复定义和重复校验：M7、M8、L1。
3. 再修测试：L3–L5。
4. 最后整理文档：L8 和 `tools/writing_lint.py` 的基线。

每项修复后运行 `python3 -m unittest discover -s tools/tests` 和 `python3 tools/writing_lint.py`。

## 首批修复（2026-10-16）

- H1：恢复默认 FFmpeg 独立比较的失败条件；原生整文件比较同时保留。
- H2：从 Jetson 原始目录补回 `manifest.json` 和 `pcm-difference.json`，本地与远程 SHA-256 一致。删除不存在的 `metrics.json` 白名单。
- M1：拒绝空输出、零范数、非有限采样和非有限比较指标。
- M2：入口要求包含 `baseline` 和至少一个不同配置；重复的 `baseline` 单独列举会失败。
- M3：比较前检查实际解码数组，空输出与非有限采样会失败。
- L3：恢复有效形状，并断言缺少输入的具体错误和解码器调用次数。原报告的触发路径更正见“子代理结论的更正”。
- L8：整理变速报告的 6 处 lint 命中，实测数据保持原样。
- H3：保留用户要求的 1.15 默认上限；人工听感任务集中记录在[变速报告的适用范围](../tempo-limit-20261016/RESULTS.md#适用范围)。

## 高优先级

| 编号 | 位置 | 问题 | 修法 | 复核 |
| --- | --- | --- | --- | --- |
| H1 | `rvc-golden/verify_tempo_playback.py:72-92` | 传入 `--helper` 时，`exact` 比较同一原生过滤器的流式输出和整文件输出。`ffmpeg_exact` 只记录，不参与失败判断。原生过滤器算错时，两边错得相同，检查通过。 | 把 `ffmpeg_exact` 放回失败条件。需要容差时，写明采样数和依据。 | 已复核 |
| H2 | `reports/tempo-limit-20261016/RESULTS.md` | 报告链接 `manifest.json` 和 `pcm-difference.json`，`.gitignore` 放行 `metrics.json`。报告目录只有 `quality-summary.json` 和 `RESULTS.md`。 | 从 `rvc-golden/tempo-115-20261016/evidence.tar` 取出这 3 个文件放入报告目录，或删除链接和白名单。 | 已复核 |
| H3 | `tools/tempo_control.py:4-5` | 默认上限改为 1.15。依据是 6 个比例 × 12 条音频，没有人工听感验收。1.125 倍的逐句音高差 P90 为 324.82 音分。 | 默认值恢复 1.05，1.15 作为可选值。补人工听感验收后再改默认值。 | 已复核 |

## 中优先级

| 编号 | 位置 | 问题 | 修法 | 复核 |
| --- | --- | --- | --- | --- |
| M1 | `rvc-golden/collect_feature_latency.py:22-31,128` | 输出全零时 `relative_rmse` 和 `cosine` 为 NaN。`> .001` 和 `< .999` 对 NaN 都为假，门控放行。 | 在 `compare` 中拒绝零范数和非有限指标。 | 已复核 |
| M2 | `rvc-golden/run_efficiency_campaign.py:53,149` | `--cases baseline` 是合法参数。`all()` 对空集合返回 `True`，报告记录 PCM 一致。 | 要求至少 2 个 case，并包含 `baseline`。 | 已复核 |
| M3 | `rvc-golden/benchmark_speech_blocks.py:40-50` | 空 WAV 能通过有限值、长度和 PCM 一致检查。 | 比较前要求样本数大于 0。 | 已复核 |
| M4 | `rvc-golden/verify_efficiency_file.py:28-34,106-115` | 基线和候选共用同一个 `--generator` 资产，只比较两份输出是否相同。错误资产也能通过。 | 核对资产 SHA-256，并与 Golden 输出比较。 | 子代理 |
| M5 | `rvc-golden/benchmark_translation_retry.py:60-63`、`benchmark_speech_features.py:138-143`、`collect_feature_latency.py:108-113` | 翻译异常写成结果字段。全部样本失败时，退出码仍为 0。 | 只捕获预期异常。失败数超过阈值时返回非零。 | 子代理 |
| M6 | `api/src/http_api.cpp:411-412` | 内存预算按 `recv` 字节计数。`body` 用 `substr` 复制，`request` 仍然保留，上传时峰值约为预算的 2 倍。 | 用 `string_view` 传递 `body`，或把复制计入预算。 | 已复核 |
| M7 | `tools/tempo_control.py:4`、`preprocessor/src/tempo_filter.c:110,127` | 1.15 上限定义了 3 处。 | C 侧使用一个宏。增加测试，比较 Python 和 C 的上限。 | 已复核 |
| M8 | `tools/speech_service.py:230`、`tools/speech_duration.py:84` | 300 s 上限在入口和内部函数各校验一次。 | 保留入口校验，删除内部重复检查。 | 已复核 |
| M9 | `tools/run_translated_speech.py:183-207` | `prepare_speech` 用 `except Exception` 捕获全部错误后继续运行。子代理报告降级原因只进入日志和进程内集合。 | 只捕获可处理的异常。把降级原因写入状态接口。 | 捕获已复核；状态接口部分为子代理结论 |

## 低优先级

| 编号 | 位置 | 问题 | 修法 | 复核 |
| --- | --- | --- | --- | --- |
| L1 | `tools/tempo_player.py:60-62` | `TempoController` 已把比例夹在 `[1, MAX_TEMPO]`。构造函数再检查一次，该分支只有测试能触发。 | 删除该检查和 `test_invalid_tempo_fails_before_opening_output`。 | 已复核 |
| L2 | `rvc-golden/verify_tempo_playback.py:21-24,57-58` | P95 索引为 `int(.95*(n-1))`。启动统计排除第 0 轮，每个后端 5 个样本，结果取第 4 小值，约为 P75。 | 使用 `statistics.quantiles`，或在输出中写明分位算法和样本数。 | 已复核 |
| L3 | `rvc-backend/tests/test_engine_chain.cpp:25-36` | 循环结束后 `inputs[0].shape` 仍为 `{INT64_MAX, 8}`。“缺少输入”用例被形状错误触发，缺少输入的检查失效时测试仍通过。 | 该用例前恢复有效形状。 | 已复核 |
| L4 | `tools/tests/test_stable_clauses.py:19-29` | 测试名称写恢复，实际用 mock 替换 `put_nowait`，只断言传给 mock 的对象。持久化失效时测试仍通过。 | 使用真实 sqlite 队列，重建 dispatcher 后读取恢复的数据。 | 已复核 |
| L5 | `tools/tests/test_tempo_player.py:49-67` | 只用合成正弦波和 2400 字节分块。报告记录 2048 和 4096 采样帧会产生尾部差异，测试覆盖不到。 | 按帧大小参数化，包含 2048 和 4096。 | 已复核 |
| L6 | `rvc-backend/src/state/file_rvc_worker.cpp:30-31,49` | 忽略 `posix_spawnattr_setflags` 和 `setpgroup` 的返回值，终止时依赖 `kill(-pid, SIGKILL)`。这两个调用通常只在参数无效时失败。子代理定为中，本报告降为低。 | 检查返回值，失败时返回错误。 | 已复核 |
| L7 | `rvc-golden/benchmark_playback_tempo.py:60-62`、`analyze_speech_blocks.py:77-79` | 跳过的样本没有计数，报告分母会变。 | 输出纳入数、跳过数和原因。 | 子代理 |
| L8 | `docs/FLEXIBLE_TEMPO.md:62`、`docs/TRANSLATION_DURATION.md`、多份报告 | 限制句分散在正文。`tools/writing_lint.py` 在 `reports/tempo-limit-20261016/RESULTS.md` 报告 6 处新命中，基线另有 470 条。 | 按 lint 逐条处理，处理后运行 `--prune`。 | lint 命中已复核；`FLEXIBLE_TEMPO.md:62` 为子代理结论 |

## 子代理结论的更正

- 第 4 个子代理报告“相对链接对应的证据文件均在工作区中找到”。该结论错误，见 H2。
- 第 3 个子代理按 6 个样本计算 P95，结果约为 P83。启动统计实际为每个后端 5 个样本，见 L2。
- 第 1 个子代理把 L6 定为中优先级。本报告按失败条件降为低。
- L3 的原始复核结论有误：`IEngine::run_chained` 先匹配输入名称，再检查形状。原用例已触发缺少输入错误；本轮改动增强测试隔离和错误断言。

## 适用范围

- 配置：主机工作区，`HEAD` 为 `c76e990`，另含未提交改动。
- 样本：18 个提交；`reports`、`*.json`、`*.log` 以外 100 个文件，新增 6,306 行。
- 原始审查方法：只读代码和静态推理。
- 首批修复配置：本机 x86_64，Python 3.10；原生测试使用独立 Ninja 构建，关闭 ONNX 和 TensorRT。
- 首批修复样本：新增 17 项 Python 测试，完整回归 206/206 通过；原生测试 25/25 通过，14 项状态测试各重复 10 次通过。写作 lint 和 diff 空白检查通过。
- M4、M5、L7 未逐行复核，M9 和 L8 部分未复核。阻塞：本轮把复核时间用于门控类问题。解除条件：修复对应项之前按行号复核。
