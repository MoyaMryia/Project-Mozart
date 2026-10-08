# 项目报告索引

本目录保存带日期的审计、实验和审查记录。报告只代表对应日期和配置，不是长期接口契约。
当前说明见 [README](../README.md)、[DESIGN](../DESIGN.md)、[TODO](../TODO.md) 和 [API](../state/API.md)。

可复用 RVC 输入、输出、张量与验证标准放在 [rvc-golden](../rvc-golden/README.md)。
历史测量、日志与人工确认原文保持原样；本轮仅增加归档提示。
编写约定见 [docs/WRITING.md](../docs/WRITING.md)。

| 日期 | 报告 | 内容 |
| --- | --- | --- |
| 2026-10-08 | [repository-audit-20261008/RESULTS.md](repository-audit-20261008/RESULTS.md) | 代码审查、HTTP/资产探针、回归结果与文档整理 |
| 2026-10-07 | [demo-20261007/RESULTS.md](demo-20261007/RESULTS.md) | 模拟音频演示与界面检查 |
| 2026-10-07 | [sentence-context-20261007/RESULTS.md](sentence-context-20261007/RESULTS.md) | 用户确认源文、人称与跨句语义限制 |
| 2026-10-06 | [reliability-20261006/RESULTS.md](reliability-20261006/RESULTS.md) | 容量、翻译与 ASR 不确定性 |
| 2026-10-06 | [chunked-speech-20261006/RESULTS.md](chunked-speech-20261006/RESULTS.md) | 短片段修复与 30 分钟测试，含质量问题 |
| 2026-10-06 | [long-speech-20261006/RESULTS.md](long-speech-20261006/RESULTS.md) / [RUN.md](long-speech-20261006/RUN.md) | 长输入与运行流程 |
| 2026-10-06 | [overload-diagnosis-20261006/RESULTS.md](overload-diagnosis-20261006/RESULTS.md) | 过载诊断；附 [GPU](overload-diagnosis-20261006/GPU.md) 与 [性能](overload-diagnosis-20261006/PERFORMANCE.md) 记录 |
| 2026-10-05 | [polish-20261005/RESULTS.md](polish-20261005/RESULTS.md) | 界面与流程整理 |
| 2026-10-05 | [full-run-20261005/RESULTS.md](full-run-20261005/RESULTS.md) | 初始全链测试与质量限制 |
| 2026-10-04 | [zero-shot-tts-redesign-20261004/PLAN.md](zero-shot-tts-redesign-20261004/PLAN.md) | 参考 TTS 路线；含候选调研与音色评估 |
| 2026-10-03 | [seedvc-zeroshot-research-20261003/RESEARCH.md](seedvc-zeroshot-research-20261003/RESEARCH.md) | Seed-VC 与 Orin Nano 离线测试 |
| 2026-10-02 | [next-steps-audit-20261002/AUDIT.md](next-steps-audit-20261002/AUDIT.md) | 合并后审计与物理演示里程碑 |
| 2026-10-02 | [pr-2-review-20261002/REVIEW.md](pr-2-review-20261002/REVIEW.md) | FAISS、RVC 导出与 Generator 诊断 |

报告中的主机路径和大型本地产物可能不在 Git 检出中。
音频、状态快照、日志和运行目录按 `.gitignore` 保存为本地证据，不能据链接存在与否推断模型已部署。
结论转为长期约定时，同步维护文档，并注明适用条件和验证证据。
