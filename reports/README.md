# 项目报告索引

本目录保存带日期的审计、实验和审查记录。报告只代表对应日期和配置，不是长期接口契约。
当前说明见 [README](../README.md)、[DESIGN](../DESIGN.md)、[TODO](../TODO.md) 和 [API](../state/API.md)。

可复用 RVC 输入、输出、张量与验证标准放在 [rvc-golden](../rvc-golden/README.md)。
历史测量、日志与人工确认原文保持原样；本轮仅增加归档提示。
编写约定见 [docs/WRITING.md](../docs/WRITING.md)。

| 日期 | 报告 | 内容 |
| --- | --- | --- |
| 2026-10-16 | [tempo-limit-20261016/RESULTS.md](tempo-limit-20261016/RESULTS.md) | 1.15 倍上限、同输入播放等待对照及代表比例质量检查 |
| 2026-10-16 | [delivery-20261016/RESULTS.md](delivery-20261016/RESULTS.md) | 待交付提交、证据清单、CI 覆盖及设备复核限制 |
| 2026-10-16 | [flexible-tempo-20261015/RESULTS.md](flexible-tempo-20261015/RESULTS.md) | 逐段比例控制、四参数时长模型、独立文本及全部默认比例扫描 |
| 2026-10-15 | [translation-duration-20261015/RESULTS.md](translation-duration-20261015/RESULTS.md) | 语音学论文和 15 篇人工中英朗读时长对照 |
| 2026-10-15 | [playback-latency-20261015/RESULTS.md](playback-latency-20261015/RESULTS.md) | 保音高变速、服务内音频过滤与称谓漏译修复 |
| 2026-10-15 | [latency-reduction-20261015/RESULTS.md](latency-reduction-20261015/RESULTS.md) | 文件整段 TensorRT、字幕事件唤醒、协作取消与小块播报 |
| 2026-10-14 | [feature-latency-20261014/RESULTS.md](feature-latency-20261014/RESULTS.md) | 当前部署功能、控制接口和执行中取消的逐项计时 |
| 2026-10-14 | [efficiency-20261014/RESULTS.md](efficiency-20261014/RESULTS.md) | GPU 张量直连、特征并行、按需加载、模型缓存与真实资产验证 |
| 2026-10-13 | [repository-audit-20261013/RESULTS.md](repository-audit-20261013/RESULTS.md) | 控制接口、文件处理、模型加载和回归覆盖修复 |
| 2026-10-09 | [realtime-patches-20261008/BOUNDARY_CLASSIFIER.md](realtime-patches-20261008/BOUNDARY_CLASSIFIER.md) | 小型中文句界分类器；单次对照降低首播等待，仍有播放积压与质量限制 |
| 2026-10-09 | [realtime-patches-20261008/SEMANTIC_GATE.md](realtime-patches-20261008/SEMANTIC_GATE.md) | 同声传译提交策略调研；0.8B 原文完整性判断短测未通过可用性检查 |
| 2026-10-09 | [realtime-patches-20261008/STABLE_CLAUSES.md](realtime-patches-20261008/STABLE_CLAUSES.md) | 稳定从句提交实验；覆盖与连续性通过，当前规则没有降低首播等待 |
| 2026-10-09 | [realtime-patches-20261008/EARLY_AUDIO.md](realtime-patches-20261008/EARLY_AUDIO.md) | 提前输出音频的固定文本对照与实时回放 |
| 2026-10-08 | [repository-audit-20261008/RESULTS.md](repository-audit-20261008/RESULTS.md) | 代码审查、HTTP/资产探针、回归结果与文档整理 |
| 2026-10-08 | [repository-audit-20261008/REALTIME_STATUS.md](repository-audit-20261008/REALTIME_STATUS.md) | 基于保存时间线的首片段生成和播放积压分析 |
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
