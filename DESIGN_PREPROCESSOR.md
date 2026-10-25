# 预处理与传输架构备忘（2026-10-25 状态）

本文记录 2026-10-25 轮架构改造后的实际形态、关键数字和运维要点。
长期设计文档见 [DESIGN.md](DESIGN.md)；运行操作见 [RUNBOOK.md](docs/RUNBOOK.md)。

## 预处理器（mozart-pre）单干形态

```
采集（--period-frames，默认 64 = 1.33 ms）
  → FL 单声道 → 80 Hz 高通 → WOLA 谱降噪（noise_suppressor.c，可旁路）
  → 整形级（噪声门/AGC/限幅，默认关闭，关闭时逐位恒等）
      ├─ 直出路由（direct）：主干 → 播放设备/USB 快流端点（10 ms 淡变防炸音）
      └─ AI 支路：960 攒帧 → 3:1 降采样 → 16k 契约帧 → MZRT（20 ms 不变）
控制通道：127.0.0.1:19040 UDP 文本协议（mode/stage/status）
```

关键实测：WOLA 发射延迟 192 样本（4.0 ms，(128,64,0) 配置）；直出软件开销
avg 5 µs；C 实现与 Python 参考（`tools/wola_reference.py`）同张量对拍 7.8e-8。
**WOLA (64,32,0) 备用配置**已验证（发射 3.33 ms、抑制 12.9–18 dB），可再省 2 ms。

## 延迟账（≤10 ms 验收口径 = Jetson 本段）

| 段 | 数值 |
| --- | --- |
| 采集 period | 1.33 ms（period 64） |
| WOLA | 4.0 ms |
| 播放 period | 1.33 ms |
| DAC/USB（估算，待声学回录终验） | ~1.5 ms |
| **合计** | **≈ 8.2 ms** ✅ |

主机回路（声音回传主机）不进 ≤10 ms 口径：2 个时钟域 + 4 个 period 边界 +
USB 往返的物理地板超 10 ms。回路定位为监视通道。

## USB 桥四层形态

| 层 | 内容 | 状态 |
| --- | --- | --- |
| 协议 | STREAM_AUDIO(20ms) + STREAM_FAST(128 样本) + CONTROL 复用同一对 bulk | 已验收（双端联调） |
| M3 端点 | gadget 接口 1（ep3/ep4）由 mozart-pre 直接持有 | 内核验证，联调 ✅ |
| 直连模式 | 主机 `MOZART_DIRECT=1` 物理设备 + 快流 | 代码就绪 |
| 守护进程 | 管道有界排空 + 四向 async SRC（水位驱动 ±500 ppm） | 已部署 |

## 时钟漂移闭环（async SRC）

环路跨两个时钟域（主机/Jetson hrtimer）。四向 async SRC（`usb-audio/src.py`，
C 参考实现 `preprocessor/src/async_src.c`）按入口水位微调线性插值比例
（±500 ppm 钳位，一阶平滑 64 块）。上线后管道积压全程 ≈0（修复前 341 ms
满管 = 740 ms 之源）；排水（FIONREAD，阈值 8 帧）降级为兜底。

## 运维要点

1. 主机守护进程运行目录：`/home/moyamryia/aec-study-20261020/`（非仓库），
   更新代码后需手工 `cp` 三个文件并 `sudo systemctl restart mozart-usb-audio`。
2. Jetson 守护直接跑仓库：`~/Mozart/usb-audio/jetson-daemon.py`，git pull + restart 即可。
3. `MOZART_FAST=0`（jetson）/`MOZART_DIRECT=0`（host）回退 20 ms 兼容模式。
4. `--usb-endpoint` 需要 `chmod 666 /dev/ffs-mozart/ep3 ep4`（重启后失效，udev 规则待固化）。
5. 测试独占 aloop 子流前先 `systemctl stop mozart-usb-audio` 并清理孤儿
   arecord/aplay（start_new_session 的子进程在守护停止后存活）。
6. 演示机服务精简开关：`tools/demo-services.sh off|on|status`。

## 演示口径

- **干净人声直出：8.2 ms（实测，零抖动）**——麦克风/耳机接 Jetson。
- 变声/字幕路径：+240 ms RVC 块（本轮未动）。
- 主机回路：监视通道，稳态 205 ms（SRC 后不漂移）。
