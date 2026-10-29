# 本机人声音色美化试听

本工具用于离线试听，不修改正在运行的实时链路。实时接线状态见[DSP 计划](../docs/FAST_VOICE_DSP_PLAN.md)。

本工具用 C 算子调整同一段人声的音色。
处理链包含低中频整理、清晰度提升、空气感提升和可选短反射。
各档输出匹配到输入的原活跃帧均方根（RMS）电平，便于直接比较音色。

清理工具处理噪声，见 [VOICE_CLEANUP_PREVIEW.md](VOICE_CLEANUP_PREVIEW.md)。
本工具改变频谱形状，补偿录音的低频过重和高频不足。

## 运行

准备单声道、PCM16 WAV，采样率为 16 kHz 或 48 kHz，长度至少 1 s。使用新输出目录。
16 kHz 输入先用 `resample_poly` 升采样到 48 kHz。

```bash
python3 tools/voice_polish_preview.py \
  "/path/to/input.wav" \
  "/path/to/new-polish-output"
```

需要 48 kHz 源时，从 `test-audio.mp4` 提取前 30 s：

```bash
ffmpeg -v error -y -i test-audio.mp4 -t 30 -ac 1 -ar 48000 \
  -c:a pcm_s16le \
  "/path/to/input-48k.wav"
```

C 算子在 `tools/voice_polish.c` 中构造系数。
处理函数只做乘加和状态更新，按 64 样本分块调用。
入口校验格式、采样率、长度和活跃帧能量。
终端限幅器上限为 −1 dBFS（线性 0.891251），取自 `mozart_limiter_init`。
电平匹配增益施加在输入侧，第二遍处理时经过限幅器。

## 三档参数

| 参数 | 02-natural | 03-bright | 04-bright-room |
|---|---|---|---|
| 高通 | 关闭 | 关闭 | 关闭 |
| 低中频 | 220 Hz、Q 1.0、−3.5 dB | 220 Hz、Q 1.0、−4.5 dB | 220 Hz、Q 1.0、−4.5 dB |
| 中低 | 500 Hz、Q 1.2、−1.5 dB | 500 Hz、Q 1.2、−2.0 dB | 500 Hz、Q 1.2、−2.0 dB |
| 清晰度 | 3.0 kHz、Q 0.8、+3.5 dB | 3.2 kHz、Q 0.8、+5.0 dB | 3.2 kHz、Q 0.8、+5.0 dB |
| 空气感 | 8 kHz 高架、+2.0 dB | 8 kHz 高架、+3.0 dB | 8 kHz 高架、+3.0 dB |
| 短反射 | 关闭 | 关闭 | 23 ms 与 34.5 ms 两抽头、5 kHz 阻尼、混入 0.12 |

`05-extreme-demo` 仅用于分辨差异：220 Hz、Q 0.8、−10 dB；450 Hz、Q 1.0、−5 dB；
3.0 kHz、Q 0.7、+9 dB；8 kHz 高架、+5 dB。

`06-extreme-gated` 用同一组低频参数，高频改为语音门控提升。
门控取 0 至 500 Hz 的 20 ms 包络与 300 ms 均值之比。
之比高于 2 时全开。`presence_hz` 与 `air_hz` 提供带通中心。
提升量：3 kHz 带通 +9 dB，4 kHz 高通 +5 dB。
包络开 20 ms、关 100 ms。前 100 ms 用累积平均初始化均值。
该档不补偿电平，保持处理链输出电平。

高通算子已实现，三个档位当前关闭。

## 实测结果

两份输入各跑一轮。

### 输入 A：`original-sample-first-30s.wav`

16 kHz、PCM16、480007 个样本，SHA-256 为
`d7dafb12f8d444b2270f2d06158f47c235ca7892aaca1513561dc9971f2a8654`。
升采样到 48 kHz 后为 1,440,021 个样本。

| 频带 | 原音 | 02-natural | 03-bright | 04-bright-room |
|---|---|---|---|---|
| 200 Hz | +11.7 dB | −3.5 dB | −4.5 dB | −4.7 dB |
| 250 Hz | +10.3 dB | −3.5 dB | −4.5 dB | −4.5 dB |
| 3.15 kHz | −13.8 dB | +3.4 dB | +4.9 dB | +4.9 dB |
| 4 kHz | −10.1 dB | +2.9 dB | +4.5 dB | +4.5 dB |
| 8 kHz | −23.0 dB | +1.6 dB | +2.6 dB | +2.6 dB |
| 10 kHz | −46.2 dB | −0.2 dB | +0.4 dB | +0.4 dB |
| 12.5 kHz | −48.8 dB | −0.9 dB | −0.5 dB | −0.6 dB |

绝对频带电平变化相对原音，含电平匹配增益：

| 档位 | 80–200 Hz | 200–500 Hz | 1–2 kHz | 2–4 kHz | 4–8 kHz |
|---|---|---|---|---|---|
| 02-natural | −0.8 dB | −1.1 dB | +2.5 dB | +4.7 dB | +3.9 dB |
| 03-bright | −1.2 dB | −1.6 dB | +3.0 dB | +6.3 dB | +5.5 dB |
| 04-bright-room | −1.4 dB | −1.5 dB | +3.0 dB | +6.4 dB | +5.5 dB |
| 05-extreme-demo | −4.3 dB | −6.3 dB | +5.6 dB | +11.0 dB | +9.3 dB |

停顿段与语音段分开测量，2000–4000 Hz 频带：

| 档位 | 停顿段 | 语音段 | 差值 |
|---|---|---|---|
| 05-extreme-demo | +7.8 dB | +7.6 dB | −0.2 dB |
| 06-extreme-gated | +0.5 dB | +4.4 dB | +3.9 dB |

200–500 Hz 在停顿段均为 −9.3 dB。

匹配增益抬高低频。原始削减量比表中数值大，差值等于匹配增益。

| 档位 | 匹配增益 | 峰值 | 两遍处理耗时 |
|---|---|---|---|
| 02-natural | 1.204 | 0.111 | 142 ms |
| 03-bright | 1.246 | 0.136 | 136 ms |
| 04-bright-room | 1.415 | 0.140 | 141 ms |
| 05-extreme-demo | 0.97 | 0.151 | 133 ms |
| 06-extreme-gated | 1.00 | 0.155 | 146 ms |

16 kHz 源在 10 kHz 以上几乎无能量。空气感档在该源上主要作用于 8 kHz 附近。

### 输入 B：`test-audio.mp4` 前 30 s

48 kHz 单声道，1,440,010 个样本。

相对 500 Hz 至 2 kHz 均值的三分之一倍频程电平变化：

| 频带 | 原音 | 02-natural | 03-bright | 04-bright-room |
|---|---|---|---|---|
| 200 Hz | +20.7 dB | −3.5 dB | −4.4 dB | −4.9 dB |
| 250 Hz | +17.9 dB | −3.6 dB | −4.6 dB | −4.7 dB |
| 3.15 kHz | −16.3 dB | +3.3 dB | +4.7 dB | +4.8 dB |
| 4 kHz | −22.5 dB | +2.5 dB | +4.0 dB | +4.0 dB |
| 8 kHz | −26.0 dB | +1.5 dB | +2.4 dB | +2.4 dB |

表中变化为处理后电平减原音电平。

| 档位 | 匹配增益 | 峰值 | 两遍处理耗时 |
|---|---|---|---|
| 02-natural | 1.294 | 0.300 | 143 ms |
| 03-bright | 1.388 | 0.348 | 133 ms |
| 04-bright-room | 1.589 | 0.365 | 138 ms |
| 05-extreme-demo | 2.135 | 0.716 | 139 ms |

计时起止点为两遍 `polish_process` 调用的墙钟时间之和，首尾闭合。
首遍取得活跃帧均方根以计算匹配增益，次遍处理增益后的输入。
每遍 22501 次 ctypes 调用，合计 45002 次；三档共 6 遍。
主机为 Intel Core Ultra 9 285H。

## 试听文件

优先播放 `00-comparison.wav`。
顺序为原音、02-natural、03-bright 和 04-bright-room。
四段电平已匹配。

| 文件 | 内容 |
|---|---|
| `01-dry.wav` | 原音，48 kHz 提取自 `test-audio.mp4` |
| `02-natural.wav` | 自然档 |
| `03-bright.wav` | 明亮档 |
| `04-bright-room.wav` | 明亮档加 23 ms 短反射 |
| `00-comparison.wav` | 四段连续比较 |
| `10-ab-*.wav` | 原音与对应档位以 2.5 s 段交替三轮，段间 0.25 s 静音 |
| `06-extreme-gated.wav` | 门控高频档，保持处理链电平 |
| `manifest.json` | 输入哈希、参数、增益、频谱变化、计时和输出哈希 |

A/B 文件取第 4 s 开始的 2.5 s，便于快速切换比较。

处理失败时输出目录只保留已写出的 WAV，并缺少 `manifest.json`。
再次运行时使用新目录。

`01-dry.wav` 为处理链的输入。16 kHz 源经升采样后写出。

## 适用范围

- 配置：Intel Core Ultra 9 285H、x86_64；Ubuntu 22.04；Python 3.10.12；NumPy 2.2.6；SciPy 1.15.3；cc 11.4.0。
- 样本：输入 A 为 16 kHz 30 秒录音，输入 B 为 48 kHz 30 秒录音，各三个档位各运行一次。
- 两份输入为不同录音。降采样到 2 kHz 后互相关的归一化峰值为 0.042；同一录音的对应段应接近 1。
- 频谱变化以 8192 点 Welch 估计和活跃帧掩膜计算。活跃帧取 20 ms 帧能量的第 60 百分位以上。
- 输入 A 的 16 kHz 采样上限为 8 kHz。8 kHz 以上内容由升采样插值产生。
- 用户听感待确认。阻塞：尚无本轮试听反馈；解除条件：完成 `00-comparison.wav` 试听并选定档位。
- 实时分块延迟、10 分钟长测和 Jetson 耗时待测。阻塞：本轮只做本机离线试听；解除条件：用户确认档位后接入实时链路。
