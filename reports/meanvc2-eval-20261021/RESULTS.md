# MeanVC2 零样本变声评估

日期：2026-10-21，新加坡时间。
评估轻量零样本变声能否替代参考音色 TTS，用于「用自己的声音播报英文」。

## 结论

音色有效，资源不够。相似度从 0.032 升到 0.610，但峰值内存 3.8–4.2 GB，
当前管线已占 2–3 GB，同机运行会超出 7 GB 板载内存。本轮不接入。

## 测试对象

| 项目 | 内容 |
| --- | --- |
| 模型 | ASLP-lab/MeanVC2，`meanvc2_120ms_40ms` |
| 参数量 | 17.7 M |
| 权重体积 | VC 70.8 MB + 声码器 33.2 MB + 内容编码器 246 MB |
| 说话人模型 | `wavlm_large_finetune.pth`，1.3 GB |
| 运行时 | Jetson Orin，torch 2.11.0+cu130 |

来源：模型来自 HuggingFace `ASLP-lab/MeanVC2` 与镜像 `hf-mirror.com`。
说话人模型在官方脚本里只给 Google Drive 链接，本轮改用
`hf-mirror.com/Dongchao/UniAudio/resolve/main/wavlm_large_finetune.pth`。
`wavlm_large_cfg.pt` 从 s3prl 的 `converted_ckpts/wavlm_large.pt` 提取，2.3 KB。

## 方法

输入为 Pocket 合成的英文试听文件 `qiqi-pocket-english.wav`，
目标音色为七七参考 `reference-qiqi-natural-source.wav`，
用 `runtime/run_rt.py --mode file` 转换，分别跑 CPU 与 CUDA。
相似度用模型自带的 WavLM-ECAPA 提取嵌入后计算余弦，转换前后使用同一套度量。

## 结果

| 指标 | CPU | CUDA |
| --- | ---: | ---: |
| 4.1 秒音频耗时 | 5.9 s | 4.4–4.8 s |
| RTF | 1.44 | 1.09–1.18 |
| 峰值 RSS | 3.76 GB | 4.20 GB |

音色相似度（与七七参考的余弦）：

| 音频 | 余弦 |
| --- | ---: |
| Pocket 英文原声 | 0.032 |
| MeanVC2 转换（CPU） | 0.610 |
| MeanVC2 转换（CUDA） | 0.599 |

## 未采用的原因

1. 峰值内存 3.8–4.2 GB，与识别、翻译、合成同时运行会超过板载 7 GB。
2. RTF 大于 1，每句播报要多等 1.1–1.4 倍音频时长。
3. 相似度用模型自身的说话人嵌入度量，训练目标与度量一致，听感需要独立确认。

## 适用范围

配置：Jetson Orin，7 GB 统一内存；torch 2.11.0+cu130；MeanVC2 `120ms_40ms` 配置；说话人模型 `wavlm_large_finetune.pth`（HF 镜像，1.3 GB）。
样本：单条 4.1 秒英文输入，CPU 与 CUDA 各 1 次；音色相似度 4 条音频对比。
该路线 2026-10-21 决定不接入，后续不再安排并发内存、听测与 `40ms` 配置对比。
