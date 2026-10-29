# 本机人声清理试听

本工具用于离线试听，不修改正在运行的实时链路。实时接线状态见[DSP 计划](../docs/FAST_VOICE_DSP_PLAN.md)。

本工具分析已确认的原始录音，并输出保守版和较强版。
复用现有高通与加权重叠相加（WOLA）降噪。保守版增加低通、动态低频衰减和能量软门。
较强版增加低能量代理谱估计和受限频谱增益。

## 运行

准备单声道、16 kHz、PCM16 WAV。录音至少为 2 s。
保留清晰语音和低能量间隔。工具要求两类帧有足够的能量差，以建立代理剖面。
使用新输出目录。

```bash
python3 tools/voice_cleanup_preview.py \
  "/path/to/input.wav" \
  "/path/to/new-cleanup-output"
```

依赖与编译方法复用 [voice_dsp_preview.py](voice_dsp_preview.py)。
C 算子在构造时计算系数。处理函数复用状态，并保持采样数。

## 试听文件

优先播放 `10-speech-rms-matched.wav`。
该文件依次比较原音、现有降噪、保守清理和较强清理。
四段统一到现有降噪版本的同一活跃帧均方根（RMS）电平。

| 文件 | 内容 |
|---|---|
| `00-original-levels.wav` | 保持各阶段原电平的连续比较 |
| `01-original.wav` | 原文件字节副本 |
| `02-current-denoise.wav` | 现有高通与 WOLA |
| `03-clean-gentle.wav` | 保守清理 |
| `04-clean-strong.wav` | 较强清理 |
| `11-*` 至 `14-*` | 独立的电平匹配版本 |
| `20-processing-difference.wav` | 现有版本与较强版的差分 |
| `90-not-selected-notch-trial.wav` | 独立静态窄陷波实验 |
| `analysis.json` | 输入哈希、代理帧定义、参数、计时及实测值 |

检查字头、尾音和清辅音。比较低频音色和高频粗糙感。

## 适用范围

本工具用于离线试听。配置、测量定义、差分解释和验收范围统一见
[30 秒录音报告](../reports/voice-cleanup-20261027/RESULTS.md#适用范围)。
