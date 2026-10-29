# 本机人声 DSP 试听

本工具用于离线试听，不修改正在运行的实时链路。实时接线状态见[DSP 计划](../docs/FAST_VOICE_DSP_PLAN.md)。

## 使用

在仓库根目录执行以下命令。使用新的输出目录。

```bash
python3 tools/voice_dsp_preview.py \
  "/path/to/input.wav" \
  "/path/to/new-dsp-output"
```

程序依赖 C 编译器、NumPy、SciPy、FFmpeg 和 espeak-ng。
程序保留原始 WAV 字节，并将输入 SHA-256 写入 `manifest.json`。
输出目录已经存在时，程序停止。

## 试听文件

本次文件位于 `~/Projects/RVC-official/output/local-voice-dsp-preview/`。

| 文件 | 内容 |
| --- | --- |
| `00-clarity-comparison.wav` | 原始、降噪、轻度清晰度、较强清晰度，带标签 |
| `10-pitch-comparison.wav` | 降噪、升 5 半音、降 5 半音、均衡加升调，带标签 |
| `01-original.wav` | 原始输入，保持原文件字节 |
| `02-denoised.wav` | 本机 C 高通与 WOLA 输出 |
| `03-clarity-light.wav` | 250 Hz 降低 2 dB，2.5 kHz 提高 2 dB |
| `04-clarity-strong.wav` | 250 Hz 降低 4 dB，2.5 kHz 提高 4 dB |
| `05-pitch-up-5.wav` | 升 5 半音，保持原时长 |
| `06-pitch-down-5.wav` | 降 5 半音，保持原时长 |
| `07-clarity-pitch-up-5.wav` | 轻度清晰度加升 5 半音 |

试听文件保留各处理分支的原始电平。
连续试听文件仅增加标签和静音间隔。

## 实现与校验

`voice_dsp_preview.c` 提供两个峰值均衡滤波器，并复用现有限幅器。
滤波系数在创建状态时计算，处理循环执行乘加和状态更新。
程序直接编译当前高通、WOLA 和整形源码，在本机处理音频。

不变速变调使用 FFmpeg 的 `asetrate → aresample → atempo`。
程序在输入末尾补静音以排空处理，再保留原时长的输出样本。
采样率取整后的实际音高倍率记录在清单中。

最初的双读头变延迟线原型在部分测试音高上出现偏差，已从试听程序移除。
当前回归使用 120、200、440 Hz，分别升降 5 半音；基频误差门槛保持为 20 cents。
其他检查覆盖滤波器数值、旁路、变长分块、reset、限幅、原文件保存及输出目录冲突。

低质量输入诊断与进一步清理见[清理试听工具](VOICE_CLEANUP_PREVIEW.md)。

## 适用范围

- 配置：本机 CPU；48 kHz 单声道处理；原始输入为用户确认的 16 kHz、480007 样本录音。
- 样本数：一份原始录音；效果输出各为 1440021 样本，时长约 30.0004 s。
- 来源：输入 SHA-256 为 `d7dafb12f8d444b2270f2d06158f47c235ca7892aaca1513561dc9971f2a8654`。
- 计时：`render_seconds` 从均衡调用前到效果完成，变调分支包含 FFmpeg 启动和管道传输。
- 预处理：清单以 64 样本块为粒度，记录首次观察到输出的输入块末端、尾部补零和裁剪数量；离线输出按输入索引对齐。
- 实时接线、缓冲延迟和听感验收范围见[实施计划](../docs/FAST_VOICE_DSP_PLAN.md#适用范围)。
