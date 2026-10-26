# 正式原生 USB 链路与四模型并发测试

日期：2026-10-26。

正式直出链路通过两轮四模型并发硬件测试。每轮运行 60 s，完整返回 45000／45000 包。
两轮共统计 89800 个稳态播放包。ALSA 整链路队列估算最大为 8.230 ms，超出 10 ms 的包数为 0。
硬件声卡的采集溢出和播放欠载均为 0。两轮回复与独立 C 参考逐样本一致。

## 正式实现

- `usb-audio/protocol.json` 统一生成 C、Python 协议定义和验收预算。
- v1 的 24 B 头保持不变。旧流使用 128 样本，新流经 HELLO、START 确认后使用 64 样本。
- 显式 START128 支持连续会话。STOP_ACK 校验全部收发计数，STOP 后额外音频仍触发失败。
- Jetson 使用有界 FIFO 保存 WOLA 的全部输出。20 ms AI 支路继续发送 MZRT 数据。
- 主机分离采集发送、USB 接收、播放和 trace 写入。队列满、超时和声卡错误立即失败。
- 主线程只更新内存状态。独立线程每 100 ms 原子写入状态文件。
- 状态互斥锁使用优先级继承。状态写入错误保留 errno，并使程序失败退出。
- 启动器使用 FIFO 40。退出时恢复旧桥服务及原 PulseAudio 状态。

运行、状态查询和回退步骤见 [native 使用说明](../../usb-audio/native-host/README.md)。

## 计时与验收

每轮发送 45000 个 64 样本块，采集源长为 60 s。
前 100 个返回包用于预热。稳态从序号 100 开始，共 44900 包。
验收工具独立核对预热数量，增加预热数量无法隐藏慢包。

USB RTT 从主机发送前取时，到主机收到对应完整回复时结束。
ALSA 整链路队列估算覆盖采集队列、USB／DSP、软件等待和播放队列：

```text
estimate_us = playback_time_us - tx_time_us
              + floor((capture_delay_frames + playback_delay_frames) × 1000000 / 48000)
```

队列读数与同一音频包关联。采集块长项与播放队列头的块长项抵消。
计时区间已包含 WOLA，不另加算法常数。
状态直方图的桶宽为 32 µs；下表的 P50、P99 为桶上界，最大值保留原始精度。

`tools/verify_native_usb.py` 检查以下条件：

1. 全部 TX／RX 序号连续，时间戳和源采样数对应。
2. 完整源与回复 WAV 等长，包含预热音频。
3. 当前 C DSP 按同一块长处理采集源，最大误差不超过 1 LSB。
4. 正常状态为 `stopped`，错误计数和声卡 xrun 为零。
5. ALSA 估算覆盖全部稳态播放包，最大值证据满足独立公式。
6. 稳态 USB RTT 和完整队列估算均满足协议定义的预算。

## 最终硬件结果

两轮均启用真实 RVC、Zipformer、PocketTTS 和 Qwen 负载。

| 记录 | 返回／发送 | 稳态播放包 | ALSA P50 | ALSA P99 | ALSA 最大值 | 估算超预算包 | 声卡 xrun |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `final-pi64` | 45000／45000 | 44900 | 7.135 ms | 7.295 ms | 8.036 ms | 0 | 0 |
| `acceptance-final64` | 45000／45000 | 44900 | 7.487 ms | 7.647 ms | 8.230 ms | 0 | 0 |

| 记录 | 稳态 USB RTT 中位数 | P99 | 最大值 | C 参考最大误差 |
| --- | ---: | ---: | ---: | ---: |
| `final-pi64` | 1.950 ms | 2.364 ms | 3.401 ms | 0 LSB |
| `acceptance-final64` | 1.961 ms | 2.387 ms | 3.412 ms | 0 LSB |

每轮比较 5760000 个 S16 声道采样值。处理使用输入 FL 通道，输出复制到双声道。
末轮启动阶段写入 7680 帧静音，预热 100 包；启动与稳态分别计数。

## 真实 AI 负载

末轮使用现有 `qiqi-zh-realtime` TensorRT front／decoder、HuBERT 和 RMVPE 引擎。
模型配置的三个 mock 开关均为 false。后端接收并返回 3000 个 20 ms 数据包，完成 250 个实时块。
推理错误、输入溢出、丢包和异源拒绝计数均为 0。

| 模型 | 末轮完成数 | 统计时间 | 输出证据 |
| --- | ---: | ---: | --- |
| Zipformer 14M，CPU 2 线程 | 150 次 | 71.692 s | 非空识别文本及有限时间戳 |
| PocketTTS int8，CPU 2 线程 | 32 次 | 71.692 s | 末次生成 72960 样本，24 kHz，RMS 0.09432 |
| Qwen3.5 0.8B Q4_K_M，GPU | 110 次 | 70.595 s | 1675 个生成 token，非空翻译文本 |

这些计数包含各负载进程的排空阶段，与主机音频的 60 s 窗口重叠。
末轮负载报告均成功。停止请求进程并排空后，再停止模型服务。

RVC 返回流报告 23 次输出欠载，其中 20 次属于启动阶段。
直出模式持续接收并计数这些返回包，监听输出使用高通＋WOLA 主干。

## 失败记录与修复依据

全部现场保留在 `rvc-golden/usb-native-20261026/`。

| 记录 | 结果 | 后续动作 |
| --- | --- | --- |
| `smoke64` | 首窗最大误差 3 LSB，独立参考验收失败 | 高通和 WOLA 关闭 fast-math 与浮点收缩，保留 1 LSB 门限 |
| `combined64` | target 3 下出现队列满和播放欠载 | 保留失败，继续分段排查 |
| `combined64-target4` | 增加一个播放周期后仍出现采集溢出和播放欠载 | 排除单纯增加缓冲作为修复方案，默认继续使用 target 3 |
| `file-combined64` | 45000 包完整返回，误差 0 LSB；18 个稳态包超预算，最大 29.443 ms | 定位实时线程中的同步状态文件写入 |
| `no-status-combined64` | 同一四模型负载下关闭文件落盘；45000 包通过，稳态最大 RTT 3.369 ms | 确认磁盘写入是首个分歧阶段 |
| `async-final64` | 恢复状态文件并使用独立 writer；硬件 45000 包通过，完整估算最大 7.877 ms | 增加优先级继承及输入资产保护后再次测试 |

收尾顺序改为先发送 USB STOP，再停止声卡采集。
SIGUSR1 定向定时器为 FunctionFS 同步 read／write 提供实际截止时间，避免只依赖 poll 就绪状态。

早期负载收尾同时停止请求与模型服务，导致最后一个 HTTP 请求断开；日志保留。
末轮改为先停止请求并等待结束，再停止模型服务，LLM 报告成功。

## 兼容性、审查与检查

同一 Jetson 进程在 64 样本会话后，连续完成两轮 128 样本会话。
每轮 3 s，返回 1125／1125 包，逐样本误差 0 LSB。

审查修复了以下问题：资产输出路径冲突、START128 会话复位、独立预热校验、重复 STOP 守卫、依赖交付和状态锁优先级反转。
原始 libusb 头文件及许可证随源码交付。写作检查仅豁免该第三方头文件的原始接口注释。

- `tools/ci-local.sh lint`：通过。
- `tools/ci-local.sh python`：415 项通过。
- `tools/ci-local.sh native`：34／34 通过，状态重复测试 14／14 通过。
- `tools/ci-local.sh tempo`：通过。
- Jetson CTest：7／7 通过。

replay 按音频／ALSA和 USB／状态分组，每组保留原有 20 s 超时，完整执行 38 个用例。
源码通过 tar 包同步，SHA-256 清单用于核对两端文件。
测试结束后，两端旧 USB 桥服务恢复 active，主机原 sink 恢复 RUNNING。
专用预处理进程及临时 AI 服务均已停止。

## 证据入口

- [末轮验收摘要](acceptance-final-summary.json)。
- [输入 WAV](../../rvc-golden/usb-native-20261026/acceptance-final64-trace/capture.wav)。
- [USB／高通／WOLA 返回 WAV](../../rvc-golden/usb-native-20261026/acceptance-final64-trace/response.wav)。
- 逐包 CSV、全部失败现场：`rvc-golden/usb-native-20261026/`。
- 负载输出及后端状态：同目录 `jetson/acceptance-final/`。
- 构建、测试和源码核对日志：同目录 `deploy-evidence/`。

## 适用范围

- 主机配置：Intel Core Ultra 9 285H，Linux 6.14.11，`hw:CARD=sofhdadsp,DEV=0` 采集与播放。
- Jetson：Orin Nano 8 GB，Linux 6.8.12-1021-tegra，RNNoise OFF，高通＋WOLA，gate／AGC／limiter 关闭。
- USB：480 Mbps bulk，interface 1；USB OUT `0x02`、IN `0x82`，FunctionFS 文件 `ep3`、`ep4`。
- 音频：48 kHz、S16、双声道、64 样本块；buffer 384 帧，prefill 256 帧，运行 target 192 帧。
- 调度：主机与 Jetson 直出进程 FIFO 40；状态写入线程使用 SCHED_OTHER 和优先级继承锁。
- 最终样本：两轮各 60 s，共 90000 个音频包，其中 89800 个稳态播放包；兼容性另测两轮 3 s。
- 物理 ADC→DAC 测量的阻塞和解除条件由[原始基线报告](../usb-latency-baseline-20261025/RESULTS.md#适用范围)统一登记。
