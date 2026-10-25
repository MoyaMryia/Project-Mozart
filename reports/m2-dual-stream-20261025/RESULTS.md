# M2：USB 桥协议双流（STREAM_FAST）

日期：2026-10-25。对应总计划 v1 的 M2 里程碑。

## 交付物

| 模块 | 内容 |
| --- | --- |
| `usb-audio/proto.py` | `STREAM_FAST=2`、`FAST_SAMPLES=128`（512 B/2.67 ms）、`FAST_SILENCE_FRAMES`；帧头零改动 |
| `usb-audio/jetson-daemon.py` | 快流泵：arecord/aplay 128 样本周期（`--period-time 2667`），上行按 STREAM_FAST 封帧；下行解复用接受双流、序号按流独立；`MOZART_FAST=0` 回退 20 ms 兼容模式 |
| `usb-audio/host-daemon.py` | 直连模式 `MOZART_DIRECT=1`：物理设备（`MOZART_DIRECT_MIC/SPEAKER`）小周期直连 + 快流收发；默认 aloop 模式行为不变，且解复用同样接受双流 |
| `tools/tests/test_usb_audio_proto.py` | 快流常量、双流交错独立序号、未知流忽略语义共 4 项新增 |

兼容性：老对端按协议设计忽略未知流（单测钉死该语义），20 ms 流全链保留。

## 验收状态

- 协议单测 10/10 通过（含新增 4 项）。
- **双端联调已执行**（2026-10-25，sudo 重启双端服务后）：
  - 链路：主机↔Jetson 双向帧流转，接口 1 被主机声明（fast=0x02/0x82），
    计数零丢帧。
  - 预处理 M2 形态（aloop 双跳）实测 6 s：MZRT 299 帧、直出 2111 周期、
    overrun 0、`direct_us` avg 5 µs。
  - 主机回路延迟（等间隔脉冲 + 逐脉冲互相关）：中位 236 ms、
    逐脉冲最小 **262 样本 = 5.5 ms**、p95 256 ms。
    逐脉冲最小 262 样本（5.5 ms）表明链路地板存在；根因与修复见
    [回路延迟排查](../loop-latency-20261025/RESULTS.md)：管道积压已修，
    稳态 205 ms（方差 <1 ms）。

## 板端延迟预算实测流程（重启后执行）

1. Jetson 启动预处理：`~/build-pre-m1/mozart-pre -d plughw:CARD=Loopback,DEV=1,SUBDEV=0 -o plughw:CARD=Loopback,DEV=0,SUBDEV=1 -a 127.0.0.1 -p 18000 --mode direct --ctl-port 19040`。
2. 主机以小周期向 `plughw:CARD=Loopback,DEV=0,SUBDEV=1` 播放脉冲测试音，从 `plughw:CARD=Loopback,DEV=1,SUBDEV=0` 录音。
3. 互相关求往返延迟；预算口径：主机 aplay/arecord 周期 + USB 0.63 ms×2 + Jetson 桥周期×2 + 预处理本段 6.7 ms。
4. 直连模式（MOZART_DIRECT=1 + 物理设备）复测，减去两侧 aloop 跳。

## 适用范围

- 配置：协议层与守护进程代码变更；单测在本机 x86_64 执行；联调在本机与 Jetson 实机。
- 样本：协议单测 10 项；预处理 6 s 直出运行 1 次；主机回路测量 3 次（等间隔脉冲 24 个、稀疏脉冲 24 个、非周期脉冲 6 个）。
- AI 流回归、20 分钟受压未执行。阻塞：本轮回测聚焦延迟。解除条件：随 soak 复测一并执行。
