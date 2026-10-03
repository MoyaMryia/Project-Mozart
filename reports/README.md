# 项目报告归档（reports）

本目录存放**带日期的项目级审查 / 审计报告**及其证据（日志、状态快照、
校验输出）。这些是一次性快照，不是长期契约；当前有效的接口、参数与流程
以 [README.md](../README.md)、[DESIGN.md](../DESIGN.md) 和
[state/API.md](../state/API.md) 为准。

Golden 回归所需的**可复用**输入 / 输出 / 张量仍然放在
[`rvc-golden/`](../rvc-golden/README.md)，不要混入本目录。

## 报告列表

| 日期 | 报告 | 内容 |
|------|------|------|
| 2026-10-02 | [next-steps-audit-20261002/AUDIT.md](next-steps-audit-20261002/AUDIT.md) | 合并 PR #1/#2 后的现状审计、下一里程碑（物理实时 demo）与优先级验收标准 |
| 2026-10-02 | [pr-2-review-20261002/REVIEW.md](pr-2-review-20261002/REVIEW.md) | PR #2（FAISS 解析 + RVC 导出验证）的代码审查与 Generator 诊断 |
| 2026-10-03 | [seedvc-zeroshot-research-20261003/RESEARCH.md](seedvc-zeroshot-research-20261003/RESEARCH.md) | Seed-VC 零样本变声调研与 Orin Nano 8GB 板上实测（离线 RTF/内存） |

## 阅读约定

- 报告中的路径、版本和内存数字是**当时快照**；后续代码演进后可能已过期。
- 报告内引用的大型音频（`*.wav`）、状态快照（`*.json`）、日志（`*.log`）与运行时
  目录（`jobs/`、`stream-*`）按 `.gitignore` 排除，只作为本地产物保留。
- 报告结论若要转为长期约定，请同步进 `DESIGN.md` / `TODO.md` /
  `state/API.md`，不要只留在报告里。
