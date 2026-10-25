# M5：dtln AI 分支增强级验证

日期：2026-10-25。对应总计划 v1 的 M5 里程碑。

## 交付物

| 内容 | 说明 |
| --- | --- |
| 模型资产 | DTLN 上游 `pretrained_model/model_1.onnx`（1.4 MB）+ `model_2.onnx`（2.4 MB），存 `rvc-golden/dtln/`；许可 MIT（上游仓库声明） |
| `rvc-golden/dtln/validate_dtln.py` | 上游 `real_time_processing_onnx.py` 的流式移植：16 kHz、block_len 512、block_shift 128（75% 重叠）、magnitude mask → 时域增益 |
| `rvc-golden/dtln/report.json` | 合成信号指标 |
| `rvc-golden/dtln/tts-results.json` | 真实 TTS 语音 SI-SDR |

## 验证结果（门限 NR>10 / 增益>0 / RTF<0.3）

| 指标 | 门限 | 实测 |
| --- | --- | --- |
| 纯噪声抑制（稳态，白噪） | ≥ 10 dB | 13.9 dB |
| 语音 SI-SDR 改善（合成语音+白噪 4.8 dB 档） | > 0 dB | **+16.0 dB**（→20.8） |
| 单核 RTF（block_shift 实时泵） | < 0.3 | x86_64 **0.021**，Jetson aarch64 **0.124** |
| 真实 TTS 语音透明（干净输入，SI-SDR） | 信息项 | 冰糖 50.8 / 苏打 47.0 / 茉莉 42.2 dB |

真实 TTS 数据佐证链路透明（干净输入的 SI-SDR 由数值精度主导）。试听文件在
`/tmp/mimo-tts/*-dtln.wav`（会话临时目录，未入库）。

## 开发记录

初版验证暴露两处实现错误并修复：(1) 分块喂入时把数据灌进补零区导致全零输出
（上游循环假设块序正确，移植时索引错位）；(2) 指标未补偿流式延迟
（block_len−block_shift=384 样本）。两者均已修正并留在脚本注释中。

## 板端 RTF（Jetson Orin，repo venv onnxruntime 1.27）

单核 RTF **0.124**，同指标数值与本机一致。门限全过，进入接线实现。
集成方案定稿：挂点 = 3:1 降采样后、MZRT 打包前（16k 域）；契约帧 320 样本
与 dtln block_shift 128 不整除，采取 dtln 内部 128 样本泵 + 320 样本对齐缓冲
（输出滞后一次对齐，代码与 M0 WOLA 相同模式）。实现排期：下一提交。

## 适用范围

- 配置：本机 x86_64 单线程 onnxruntime；合成信号 + 真实 TTS 语音。
- 样本：合成 8 s ×1；真实 TTS 3 段（各 12–19 s）。
- 音乐透明（dtln 为语音模型，不承诺音乐口径，音乐透明由 fast 档经典链承担）、
  ASR 字错率闭环未执行。阻塞：接线实现未完成。解除条件：接线后按同类电池补测。
