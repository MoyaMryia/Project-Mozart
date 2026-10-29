# Mozart 文档导航

本目录收录长期维护的说明。历史实测记录在 [reports/](../reports/README.md)。

## 先读这几份

| 文档 | 内容 |
| --- | --- |
| [../README.md](../README.md) | 项目概览、构建、启动与当前状态 |
| [RUNBOOK.md](RUNBOOK.md) | 部署、启动、健康检查、回滚与故障处置 |
| [../TARGET.md](../TARGET.md) | 产品目标与验收范围 |
| [../DESIGN.md](../DESIGN.md) | 架构与数据契约 |
| [../TODO.md](../TODO.md) | 剩余问题与优先级 |

## 专题说明

| 文档 | 内容 |
| --- | --- |
| [WRITING.md](WRITING.md) | 英文 STE 与中文编写约定 |
| [FLEXIBLE_TEMPO.md](FLEXIBLE_TEMPO.md) | 逐段变速与四参数时长模型 |
| [TRANSLATION_DURATION.md](TRANSLATION_DURATION.md) | 译文时长控制的方案与证据 |
| [REALTIME_LATENCY.md](REALTIME_LATENCY.md) | 实时链路的延迟构成 |
| [SIMULTANEOUS.md](SIMULTANEOUS.md) | 可选稳定从句与句界分类器实验 |
| [../DESIGN_PREPROCESSOR.md](../DESIGN_PREPROCESSOR.md) | 预处理、路由、设备控制与 USB 传输 |
| [../tools/REALTIME_VOICE.md](../tools/REALTIME_VOICE.md) | 日常实时入口、输出选择和日志 |
| [../tools/REFERENCE_SPEECH.md](../tools/REFERENCE_SPEECH.md) | 参考播报、队列、恢复与 API |
| [FAST_VOICE_DSP_PLAN.md](FAST_VOICE_DSP_PLAN.md) | 轻量人声 DSP 的实时接线计划 |
| [../tools/VOICE_DSP_PREVIEW.md](../tools/VOICE_DSP_PREVIEW.md) | 离线均衡与变调试听 |
| [../tools/VOICE_CLEANUP_PREVIEW.md](../tools/VOICE_CLEANUP_PREVIEW.md) | 离线噪声诊断与清理 |
| [../tools/VOICE_POLISH_PREVIEW.md](../tools/VOICE_POLISH_PREVIEW.md) | 离线人声音色美化 |

## 接口与部署

| 文档 | 内容 |
| --- | --- |
| [../state/API.md](../state/API.md) | HTTP 接口与字段 |
| [../usb-audio/README.md](../usb-audio/README.md) | USB 音频桥的安装与设备映射 |
| [../frontend/DEPLOYMENT.zh-CN.md](../frontend/DEPLOYMENT.zh-CN.md) | 控制台部署 |

## 报告的读法

`reports/<主题>-<日期>/RESULTS.md` 按时间排列，每份末尾有「适用范围」一节，
写明配置与样本数。未采用路线的报告保留结论，不再更新数据。

近期功能与验证：

| 报告 | 结论 |
| --- | --- |
| [../reports/dual-live-20261028/RESULTS.md](../reports/dual-live-20261028/RESULTS.md) | 单采集双发的板端验证 |
| [../reports/asr-selection-20261028/RESULTS.md](../reports/asr-selection-20261028/RESULTS.md) | 复核选择与固定文本翻译对照 |
| [../reports/refinement-deadline-20261028/RESULTS.md](../reports/refinement-deadline-20261028/RESULTS.md) | 复核窗口的本地调度回归 |
| [../reports/speech-delivery-semantics-20261028/RESULTS.md](../reports/speech-delivery-semantics-20261028/RESULTS.md) | 独立投递、持久去重与语义反例 |
| [../reports/speech-latency-20261029/RESULTS.md](../reports/speech-latency-20261029/RESULTS.md) | 历史时间线重算与提前输出入口 |
| [../reports/control-races-20261029/RESULTS.md](../reports/control-races-20261029/RESULTS.md) | 设备草稿、停止控制和旧请求拒绝 |
| [../reports/frontend-simplification-20261029/RESULTS.md](../reports/frontend-simplification-20261029/RESULTS.md) | 页面层级、试听结果与错误显示 |
| [../reports/usb-audio-20261020/RESULTS.md](../reports/usb-audio-20261020/RESULTS.md) | USB 全双工音频桥验证 |
| [../reports/translation-model-20261021/RESULTS.md](../reports/translation-model-20261021/RESULTS.md) | 翻译模型保留 0.8B |
| [../reports/refinement-wait-20261021/RESULTS.md](../reports/refinement-wait-20261021/RESULTS.md) | 复核等待窗口保持 2.5 秒 |
| [../reports/meanvc2-eval-20261021/RESULTS.md](../reports/meanvc2-eval-20261021/RESULTS.md) | MeanVC2 音色有效但内存不够，不接入 |
