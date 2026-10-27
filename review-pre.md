# 预处理链路复核记录

最后更新：2026-10-26
排序：从新到旧
范围：工作站虚拟声卡、USB bulk 链路、Jetson `mozart-pre`。不含 RVC、ASR、参考播报和前端。

## 2026-10-26 当前基准

虚拟声卡和私有图工作暂停。后续测试以 Jetson card 0 物理直入直出为基准。

统一前提：

- 延迟数字扣除虚拟声卡和 PipeWire 图开销。
- 共享虚拟图的 25–90 ms 单独记录，不混入物理直出结果。
- 10 ms 只用于物理直出或私有图；共享图只作兼容模式。

当前结论：

| 路径 | 延迟量级 | 状态 |
| --- | ---: | --- |
| Jetson card 0 物理回环 | 10.4–11.4 ms | 已测，不含 WOLA |
| Jetson card 0 + WOLA | 约 14.4–15.4 ms | 按 4.0 ms 发射延迟叠加 |
| 主机 USB 往返 | 1.5–2 ms 典型，3.4–4.7 ms 最大 | 已测，仅传输与预处理块 |
| 主机 ALSA 整链 | 7.1–8.2 ms | 已测 |
| 共享虚拟声卡 | 25–90 ms | 已测图周期，链路调试中 |
| 私有图 64 帧 | 图本体 64 帧 | host 链路仍 `queue full` |

按当前 card 0，Jetson 物理直入直出预计超过 10 ms。

## 2026-10-26 Jetson card 0 实测

测量对象：

```text
card 0: USB PnP Sound Device (C-Media)
48 kHz、双声道 S16、period 64、buffer 1024
```

方法：

- 先发 0.4 s 静音，再发单样本脉冲。
- 同时采集回环，统计峰值样本位置。
- 延迟按峰值位置减脉冲位置计算。

结果：

| 运行 | 峰值位置 | 回环延迟 |
| --- | ---: | ---: |
| 1 | 19747 | 11.396 ms |
| 2 | 19699 | 10.396 ms |
| 3 | 19700 | 10.417 ms |

该数值覆盖声卡 DAC/ADC、USB Audio 和 ALSA 队列，不含 WOLA。
`buffer 192` 时出现欠载和负延迟，数据丢弃。

`mozart-pre` 的完整 card 0 回环还没有跑。该程序采集缓冲为 4 个 period，播放缓冲至少为 period + 960 帧，实际值需要单独测量。

## 2026-10-26 私有图评估

启动方式：

```text
私有 pipewire + 私有 WirePlumber
Dummy-Driver
default.clock.quantum = 64
两个空 sink：mozart_speaker、mozart_mic
```

结果：

| 项目 | 结果 |
| --- | --- |
| 私有图 quantum | 64 帧 |
| `Mozart Speaker/Mic` | running |
| `pw-play` 到私有 sink | 成功 |
| 系统音频 | 不受影响 |
| `virtual-exclusive` host | `queue full` |
| 采集溢出 | 207 次 |
| 播放 ack | 到 34 块停止 |

私有图本体可到 64 帧。host 的采集和播放私图连接还在调试。
10 ms 的私有图口径为：私有 sink 写入到私有 source 读出；不含应用自身缓冲。

## 2026-10-26 选项 B：单 `pw_filter`

PoC：

- 一个输入端口、一个输出端口。
- 测试 `Audio/Sink` 和 `Audio/Duplex`。
- 测试 `RT_PROCESS` 和 `DRIVER | RT_PROCESS`。

结果：

| 检查项 | 结果 |
| --- | --- |
| filter 状态 | `connecting` → `paused` |
| `node id` | `0xFFFFFFFF` |
| `pactl` 设备 | 无 |
| 进程回调 | 0 次 |
| `pw-dump` 节点 | 存在，状态 `None`/`suspended` |

当前版本未形成可用设备，暂缓采用。

## 2026-10-26 共享图与 ALSA 周期

ALSA 规则：

```text
api.alsa.period-size = 64
api.alsa.periods     = 3
api.alsa.headroom    = 0
```

结果：

| 配置 | 图 quantum | 系统音频 |
| --- | ---: | --- |
| 默认 | 2048 帧 | 正常 |
| 上述规则 | 512 帧 | 正常 |
| `periods=1` + 全局 quantum 64 | 64 帧目标 | `paplay` 超时 |

结论：当前 SOF 驱动下，共享图最低为 512 帧；64 帧会使物理播放超时。
规则已回滚，默认配置保留。

## 2026-10-26 选项 A：拆播放子进程

实现：

- 主进程保留 PipeWire 采集和 USB。
- 播放交给子进程，通过 Unix socket 传 64 帧块并回 ack。
- 新增 `socket_playback.cpp`、`playback_helper.cpp`。
- 主机参数加入 `--audio-mode direct|virtual-shared|virtual-exclusive`。

结果：

| 项目 | 结果 |
| --- | --- |
| 采集流链接 | `mozart_speaker:monitor` 正确 |
| 播放流链接 | `mozart_mic:playback` 正确 |
| ack | 到 32–34 块 |
| 失败 | `queue full` |
| 原因 | 共享图 512–1024 帧，子进程消费速度不足 |

拆进程解决了同进程双流停止排空的问题。共享图周期仍然限制消费速度。

## 2026-10-26 PipeWire 升级与虚拟设备

升级结果：

| 组件 | 版本 |
| --- | --- |
| PipeWire | 1.0.7 |
| WirePlumber | 0.5.2 |
| 音频服务器 | PulseAudio on PipeWire 1.0.7 |

虚拟设备：

- `Mozart Speaker`：48 kHz 空 sink。
- `Mozart Mic`：48 kHz 空 sink，应用从 monitor 录音。
- 配置文件：`usb-audio/pipewire/99-mozart-virtual.conf`。
- 系统音频、`paplay`、`parec` 正常。

失败路径：

- `pw-loopback` 改 `media.class`：播放流阻塞。
- 同进程双 PipeWire 流：播放环停止排空。
- 全局 quantum 64：物理声卡播放超时。

## 2026-10-25 直出与 USB 基线

主机 USB 往返，含高通和 WOLA 处理：

| 数据集 | p50 | p99 | 最大值 |
| --- | ---: | ---: | ---: |
| `final-pi64`，45000 包 | 1.950 ms | 2.364 ms | 3.401 ms |
| `acceptance-final64`，45000 包 | 1.961 ms | 2.387 ms | 3.412 ms |
| 已连接复测，45000 包 | 1.455 ms | 1.591 ms | 1.937 ms |
| 基线 DSP，9000 包 | 1.527 ms | 1.691 ms | 4.745 ms |

主机 ALSA 整链：

| 指标 | 结果 |
| --- | ---: |
| p50 | 7.135–7.487 ms |
| 最大值 | 8.230 ms |
| 超 10 ms 包 | 0 / 89800 |

direct 模式长跑事故：

- 运行约 450 s 后出现 `queue full`。
- 播放欠载 1 次。
- 当时 RTT 最大值 13.13 ms。
- 该事故说明 long-run 最大值需要单独验收。

## 2026-10-25 方案选择

已选：

| 项目 | 选择 |
| --- | --- |
| 主机虚拟设备 | PipeWire 空 sink |
| 共享模式 | 系统 PipeWire 图 |
| 独占模式 | 私有 PipeWire 图 |
| 播放路径 | 独立子进程 + Unix socket ack |
| 预处理 | 80 Hz 高通 + WOLA |
| 整形级 | 噪声门、AGC、限幅关闭 |
| RNNoise | 关闭 |
| 时钟 | 主机图驱动；USB 快流不做 SRC |
| 基线 | `direct` ALSA 物理设备 |

不采用：

- `snd-aloop` 旧桥：多一个时钟域，SRC 和 Python 调度增加延迟。
- `pw-loopback` 直接改 `media.class`：播放流阻塞。
- 同进程双 PipeWire 流：播放环停止排空。
- 全局强制 64 帧：物理声卡播放超时。
- USB 快流加 SRC：当前路径不需要跨时钟域重采样。
- 当前 `pw_filter` 双端口：PoC 未形成设备。

## 之后的核对口径

### 通用前提

- 所有延迟测试扣除虚拟声卡和 PipeWire 图开销。
- 物理直出和 USB 直出分别记录，不混合。
- 共享虚拟图只作兼容模式，不进入 10 ms 验收。

### 测试端点

| 路径 | 起点 T0 | 终点 T1 |
| --- | --- | --- |
| Jetson card 0 | 脉冲写入 card 0 播放 | card 0 采集到对应峰值 |
| Jetson card 0 + `mozart-pre` | card 0 采集到输入样本 | card 0 播放出对应输出样本 |
| 主机 USB 直出 | 主机发送 64 帧块 | 主机收到对应 64 帧块 |
| 私有图 | 应用写入私有 `Mozart Speaker` | 应用从私有 `Mozart Mic` 读出 |

### 固定配置

- 采样率：48 kHz。
- 格式：双声道 S16。
- 块长：64 帧。
- 预处理：80 Hz 高通，WOLA 开启，噪声门/AGC/限幅关闭，RNNoise 关闭。
- 设备：Jetson card 0 使用 `plughw:CARD=Device,DEV=0`，period 64。
- 主机直出使用 FIFO 40；Jetson direct 使用对应直出进程。

### 必测指标

- p50、p99、最大值。
- 连续运行 10 分钟。
- 发送块数、接收块数、样本数。
- 采集溢出、播放欠载、`queue full`、USB 错误计数。
- 原始日志、状态文件和 trace 路径。

### 通过条件

- 最大值小于 10 ms。
- 发送与接收块数一致。
- 采集溢出和播放欠载为 0。
- `queue full` 为 0。
- 对拍误差不超过 1 个 S16 量化单位。

### 记录字段

每轮记录：

- 日期、主机、Jetson、内核、PipeWire/WirePlumber 版本。
- 命令、配置文件、period、buffer、目标块长。
- 开始与结束时间、样本数、统计区间。
- p50、p99、最大值和失败计数。
- 原始日志与结果文件路径。

## 适用范围

- 配置：PipeWire 1.0.7、WirePlumber 0.5.2、48 kHz、64 帧目标块；Jetson card 0 为 `USB PnP Sound Device`，period 64、buffer 1024。
- 样本：card 0 回环 3 次；USB RTT 来自 45000 包和 9000 包数据集；ALSA 整链 89800 包；direct 长跑约 450 s。
- Jetson card 0 的 `mozart-pre` 全路径当前未测；阻塞：需要 card 0 独占回环和 10 分钟运行；解除条件：跑通并统计最大值。
- 私有图最大值当前未测；阻塞：host 在 `virtual-exclusive` 下仍 `queue full`；解除条件：采集/播放私图连接闭环后完成 10 分钟测量。
- 共享虚拟图链路调试中；阻塞：图 quantum 为 512–1024 帧；解除条件：64 帧驱动或私有图方案通过。
- 选项 B 设备暴露当前未通过；阻塞：`pw_filter` 节点 paused 且 node id 无效；解除条件：补充端口和设备属性后复测。
