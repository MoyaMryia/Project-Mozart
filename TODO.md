# 剩余工作

更新日期：2026-10-24。当前实现见 [DESIGN.md](DESIGN.md)，部署与运维见 [docs/RUNBOOK.md](docs/RUNBOOK.md)。

## 交付前必须

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 译文质量盲听 | 待做 | 现有数据只有检查通过率与延迟，判断质量要人听 |
| Windows 真机验证 | 待做 | `usb-audio/windows-bridge.py` 在 Linux 验证过，Windows 需装 WinUSB 驱动，步骤见 [USB 音频桥](usb-audio/README.md#windows-主机) |
| 现场声卡适配 | 待做 | 设备名按现场修改，USB 音频桥可免去这一步 |

## 可以继续打磨

- 直出路径板端实测：单干重构已合入，用控制通道 `status` 的 `direct_us` 实测延迟并试听切换，见[报告](reports/m1-single-trunk-20261025/RESULTS.md)。
- M2/M3 部署重启与联调：代码已合入，两端服务以 root 运行需 sudo 重启后实测，见[报告](reports/m2-dual-stream-20261025/RESULTS.md)。
- M4 第 2-3 步：控制台 WS 化、FILE 子协议、内部服务 Unix 化，见[报告](reports/m4-control-plane-20261025/RESULTS.md)。
- M5 dtln 集成：AI 分支 16 kHz 神经级，待模型资产下载与 Jetson RTF 实测。
- M6 彩排：按[应急手册](docs/REHEARSAL.md)跑三轮。

- 长测丢帧修复待板端复测：已删除积压突发丢弃并将实时环容量加至 32 帧，需重跑完整文件长测确认零丢帧，见[失败记录](reports/soak-20261019/RESULTS.md)。

- 翻译失败率：最近一次 30 分钟受压为 367 段中 11 段失败，见[报告](reports/usb-audio-20261020/RESULTS.md)。
- 字幕切碎：结束判定 0.4 秒比 0.6 秒早 0.2 秒出声，但片段长度下降 23%，见[复核等待报告](reports/refinement-wait-20261021/RESULTS.md)。
- USB 守护进程：`arecord` 的 stderr 用管道接收、控制帧只做自测，未进入握手流程、以 root 运行。
- 2B 翻译模型：通过率与 0.8B 相同、延迟高 60%，需要时可用，见[模型对比](reports/translation-model-20261021/RESULTS.md)。

## 本轮已完成

- USB 全双工音频桥，取代模拟回路，见[报告](reports/usb-audio-20261020/RESULTS.md)。
- 控制台静态服务与 `/api` 代理，部署步骤见[运行手册](docs/RUNBOOK.md#控制台)。
- 术语表可配置，含配置页与同音纠错，见[设计](DESIGN.md#6-字幕与参考音色播报)。
- 口语年代、凭空否定、断裂数字三处文本修复。
- 变速上限 1.50 倍，并修掉演示机上过期的变速库。
- 早播音频运行时，首块音频就绪 0.29 秒。
- 部署脚本、冒烟测试、运行手册与文档导航。

历史问题清单与修复证据见 [reports/README.md](reports/README.md)。
