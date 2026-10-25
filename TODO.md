# 剩余工作

更新日期：2026-10-25。当前实现见 [DESIGN.md](DESIGN.md)，部署与运维见 [docs/RUNBOOK.md](docs/RUNBOOK.md)。

## 交付前必须

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 译文质量盲听 | 待做 | 现有数据只有检查通过率与延迟，判断质量要人听 |
| Windows 真机验证 | 待做 | `usb-audio/windows-bridge.py` 在 Linux 验证过，Windows 需装 WinUSB 驱动，步骤见 [USB 音频桥](usb-audio/README.md#windows-主机) |
| 现场声卡适配 | 待做 | 设备名按现场修改，USB 音频桥可免去这一步 |

## 可以继续打磨

- 端到端声学终验：软件链 8.2 ms 已实测，DAC/USB 段待 PCM2902 声学回录，见[报告](reports/e2e-latency-20261025/RESULTS.md)。
- dtln 接线 AI 分支：C 集成已对拍 1e-8，接线前需把标量 DFT 换 FFT（当前 2.5 ms 贴块周期线），见[报告](reports/m5-dtln-20261025/RESULTS.md)。
- M4 第 2-3 步：控制台 WS 化、FILE 子协议、内部服务 Unix 化，见[报告](reports/m4-control-plane-20261025/RESULTS.md)。
- udev 规则固化 `/dev/ffs-mozart/ep*` 权限（现每次重启后手工 chmod）。
- 主机守护进程运行目录迁回仓库部署路径（现在手工同步到 aec-study-20261020）。
- M6 彩排：自动化子集已过，现场人工三轮（拔插/听感/断网）按[手册](docs/REHEARSAL.md)。
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
