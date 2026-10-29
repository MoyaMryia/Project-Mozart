# 纯标点识别输出过滤

## 结果

ASR 出口过滤仅包含空白和 Unicode 标点的结果。
过滤同时作用于 JSON 事件和普通控制台输出。
带文字或数字的短句继续通过，包括“不”“对”“了”和“0”。
混合文本保留原标点，例如“你好。”。
独立符号 `$` 和 `+` 保持原样。

纯标点 final 在纠错、SenseVoice 复核和翻译交付之前被过滤。
过滤继续推进 `cut_offset`，并保持后续有效 final 的连续序号。
识别流的端点重置和超时关闭逻辑保持原样。

过滤日志使用 `skipped punctuation-only final`。
`audio-status.json` 的 `punctuation_finals_skipped` 记录累计过滤数量。
空白结果直接忽略。

## 校验

- STT 入口测试：11 项通过。
- Python 回归：624 项，619 项通过，5 项跳过。
- 测试覆盖 partial、final、普通控制台、复核提交、超时关闭和重新开流。
- 绕过标点过滤后，原入口集成测试失败。
- 将过滤改为只保留字母后，短词与数字测试失败。

```bash
python3 -m unittest discover -s tools/tests -p test_stt_telemetry.py -v
bash tools/ci-local.sh --staged
```

本次保留低内存保护阈值。
模型及运行范围见 [X-ASR 对照报告](../xasr-streaming-20261029/RESULTS.md)。

## 适用范围

- 配置：Mock ASR、Mock UDP、16 kHz 单声道输入，每帧 320 个样本；生产识别出口使用同一过滤函数。
- 样本：新增 4 项测试，覆盖 22 个固定字符串与 18 个合成契约帧；全部 STT 入口测试共 11 项。
- 过滤条件：仅删除纯空白和纯 Unicode 标点结果，含其他字符的文本保留原样。
- 实际模型对照、现场录音标注及长会话测试范围见上方 X-ASR 报告。
