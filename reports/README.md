# 项目报告索引

本目录保存带日期的审计、实验和审查记录。报告只代表对应日期和配置，不是长期接口契约。
当前说明见 [README](../README.md)、[DESIGN](../DESIGN.md)、[TODO](../TODO.md) 和 [API](../state/API.md)。

可复用 RVC 输入、输出、张量与验证标准放在 [rvc-golden](../rvc-golden/README.md)。
历史测量、日志与人工确认原文保持原样；本轮仅增加归档提示。
编写约定见 [docs/WRITING.md](../docs/WRITING.md)。

| 日期 | 报告 | 内容 |
| --- | --- | --- |
| 2026-10-25 | [loop-latency-20261025/RESULTS.md](loop-latency-20261025/RESULTS.md) | 主机回路延迟排查；管道积压根因坐实与有界排空修复，SRC 落地后稳态不漂移 |
| 2026-10-25 | [e2e-latency-20261025/RESULTS.md](e2e-latency-20261025/RESULTS.md) | 端到端延迟达标：Jetson 实机双电缆脉冲实测，period 64 产品口径 8.2 ms 零抖动 |
| 2026-10-25 | [m5-dtln-20261025/RESULTS.md](m5-dtln-20261025/RESULTS.md) | dtln AI 分支：ONNX 链验证门限全过、C 集成双端对拍 1e-8 级 |
| 2026-10-25 | [m4-control-plane-20261025/RESULTS.md](m4-control-plane-20261025/RESULTS.md) | 控制面迁移第一步：二进制消息契约与 Unix 数据报服务并存 |
| 2026-10-25 | [m3-endpoints-20261025/RESULTS.md](m3-endpoints-20261025/RESULTS.md) | 快流专用端点：描述符双接口、预处理器直占 ep3/ep4，内核验证 |
| 2026-10-25 | [m2-dual-stream-20261025/RESULTS.md](m2-dual-stream-20261025/RESULTS.md) | USB 桥协议双流：STREAM_FAST 128 样本快流与主机直连模式，双端联调 |
| 2026-10-25 | [m1-single-trunk-20261025/RESULTS.md](m1-single-trunk-20261025/RESULTS.md) | 预处理器单干重构：WOLA 替换 RNNoise、直出路由、控制通道，含板端冒烟 |
| 2026-10-25 | [wola-m0-20261025/RESULTS.md](wola-m0-20261025/RESULTS.md) | WOLA 谱降噪参考实现与验证电池：全门限通过，真实 TTS 复验 |
| 2026-10-25 | [rehearsal-20261025/RESULTS.md](rehearsal-20261025/RESULTS.md) | M6 彩排自动化子集：冷重启自愈、切换压力、断链存活、60 s 稳态 |
| 2026-10-20 | [demo-readiness-20261020/RESULTS.md](demo-readiness-20261020/RESULTS.md) | 稳定运行候选；启动、音频故障、配置与客户端归属修复，含板端 1000 帧短测和 USB 检查 |
| 2026-10-20 | [tempo-cap-20261020/RESULTS.md](tempo-cap-20261020/RESULTS.md) | 变速上限提高至 1.50 倍、积压需求分布及高比例识别对照 |
| 2026-10-19 | [product-readiness-20261019/RESULTS.md](product-readiness-20261019/RESULTS.md) | 产品交付审核；主链路、故障恢复、部署、资源和验收缺口，含 5 个定向探针 |
| 2026-10-19 | [backlog-fix-20261019/RESULTS.md](backlog-fix-20261019/RESULTS.md) | 175 分段重放；数字误拒与失败入队修复，峰值 47.55→23.20 s，保留翻译及合成失败 |
| 2026-10-19 | [diagnostic-20261019/RESULTS.md](diagnostic-20261019/RESULTS.md) | 完整独立变声与静态播报；逐段中英文时长、翻译重复、合成尾段及播放预算 |
| 2026-10-19 | [soak-20261019/RESULTS.md](soak-20261019/RESULTS.md) | 完整文件组合长测因后端丢帧失败；保留积压、翻译、播报与音频核验记录 |
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
