# 识别复核选择与翻译回归

本次修改修复了已记录的专名丢失、短句复核被拒和字幕源文本不一致。
独立翻译对照中，修改后有 44/44 条通过现有文本校验，基线为 43/44 条。
完成状态与下面的语义示例分开列出。

## 代码修改

- 用短句专用相似度门限替代长度一票否决。选择规则见[识别不确定性](../../tools/REFERENCE_SPEECH.md#recognition-uncertainty)。
- 保留原文中术语的出现次数。内置翻译词条与自动同音替换词条分开，避免把普通「元神」自动改成游戏名。
- 相近候选的显著数量冲突、严格情态极性冲突和仅有否定词增减的冲突会阻止翻译与播报。
- 采用复核文本时，翻译同时接收该复核的数字与极性审核。复核失败或跳过时立即唤醒翻译线程。
- 主字幕使用实际译文源。迟到的复核结果只显示为候选。
- 复核与选择阶段的配置错误记入状态。选择代码的内部错误停止工作线程，并传递给桥接主线程。
- 新增排队、复核等待和最终识别后总耗时。恢复磁盘任务时清空旧计时。字段定义见[状态接口](../../state/API.md#captions-parameters-and-speech)。

## 独立翻译对照

固定文本来自 `realtime-20261028-204620-119985/subtitles.jsonl`。
[文本样本](../../tools/tests/fixtures/asr-selection-20261028.json)保存 44 条最终记录和原日志的 SHA-256。
原日志位于 Jetson 归档目录，本轮已重新核对其校验值。
基线使用 `a7bb966` 的翻译函数及日志中的实际译文源。
修改后路径重新选择译文源，再调用翻译函数。
两条路径使用同一个独立模型进程。奇数序号先运行修改后路径，偶数序号先运行基线。

以下文本对照覆盖选出的 7 条记录：

| 序号 | 原始识别 | 基线输出 | 修改后输出 |
| --- | --- | --- | --- |
| 8 | 原神是一款游戏 | Neuro Spirit is a game. | Genshin Impact is a game. |
| 16 | 中东英文；复核为「中英文」 | Middle East English | Chinese and English. |
| 17 | 中英文 | 校验拒绝：英文残留中文 | Chinese and English. |
| 39 | 我麻了兄弟 | I'm shocked at your brother | I am stunned brother. |
| 41 | 我们对此无能为力 | We cannot do this. | We are powerless against this. |
| 42 | 非常遗憾的是英国拒绝了我们的条款 | Sincere regret, we have been refused our terms by the State. | Sincere regret that the United Kingdom refused our terms. |
| 43 | 同上一条 | Unfortunately, the United Kingdom refused our terms. | Unfortunately, the United Kingdom refused our terms. |

第 39 条消除了「your brother」引入的关系。该输出保留了口语称呼，英文标点仍较生硬。
第 42 条保留了「英国」。第 43 条的既有正确输出保持不变。

## 计时

计时从调用 `translate_recognized_event` 开始，到函数返回或校验拒绝结束。
统计包含所有 44 条记录的翻译、文本检查和重试。
P95 取排序索引 `floor(0.95 × (n − 1))`。

| 路径 | 中位数 | P95 | 最大值 |
| --- | ---: | ---: | ---: |
| 基线 | 192 ms | 377 ms | 448 ms |
| 修改后 | 213.5 ms | 339 ms | 442 ms |

本次主要收益是输入选择与译文质量。两组中位耗时接近，修改后中位耗时略有增加。
复核终态唤醒的单元测试覆盖 `failed` 和 `skipped`，没有等待满窗口。

## 复现

在 Jetson 仓库根目录准备独立测试目录：

```bash
mkdir -p /tmp/mozart-asr-baseline /tmp/mozart-asr-offline
git archive a7bb966 tools | tar -x -C /tmp/mozart-asr-baseline
cp tools/subtitle_bridge.py tools/asr_checks.py /tmp/mozart-asr-offline/
cp tools/tests/fixtures/asr-selection-20261028.json /tmp/mozart-asr-offline/records.json
.venv/bin/python rvc-golden/asr-selection-20261028/probe.py
```

[调查脚本](../../rvc-golden/asr-selection-20261028/probe.py)启动独立的本机翻译进程，端口为 `19200`。
脚本结束时回收该进程。脚本不启动麦克风、后端或播报服务。
[完整记录](../../rvc-golden/asr-selection-20261028/results.json)保留两条路径的源文本、输出、审核和耗时。
调查脚本检查全部候选的完成状态，并检查表中 7 条的预期词条。
将这些输出改回原错误，或删除第 43 条的国家名，会使对应检查失败。

## 适用范围

- 配置：Jetson Orin Nano Super 8 GB，Qwen3.5 0.8B `Q4_K_M`，CUDA，单槽，文本上下文 2048；完整模型命令保存在对照记录中。
- 复现前提：Jetson 的 `.venv`、模型文件、CUDA 运行库和演示配置保持可用。其他路径使用脚本参数覆盖。
- 证据中的绝对路径记录实测主机位置。脚本从当前仓库根目录定位源码和配置。
- 样本：一次独立对照，44 条固定日志文本；其中 7 条作上述文本核对。
- 源文本正确性待核对。阻塞：缺少人工确认的录音转写。解除条件：核听保存的录音并提供对应转写。
- 实时端到端延迟待测量。阻塞：本轮保持实时服务栈停止。解除条件：获准启动后，用同一录音测量语音结束至页面英文出现的耗时。
- 本轮保留原有断句和整句提交设置，未启用稳定短句提前翻译。
