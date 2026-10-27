# 字幕链路 30 秒实测

日期：2026-10-27。

字幕链路已经跑通：识别、翻译、事件流、控制台代理都产出数据。
翻译质量有 5 类问题，其中 2 类导致字幕为空。

## 配置

| 环节 | 组件 | 参数 |
| --- | --- | --- |
| 识别 | `sherpa-onnx` zipformer-zh-14M | 流式 |
| 定稿 | SenseVoice zh-en-ja-ko-yue int8 | 句子结束 |
| 翻译 | `llama-server` + qwen35-0.8b-text-Q4_K_M | CUDA，batch 256，cache 128 MiB |
| 后端 | `mozart_stated` | `state/translated-speech.yaml`，RVC 关闭，端口 18080 |
| 字幕桥 | `tools/subtitle_bridge.py` | `--delivery-policy coverage` |
| 运行环境 | `~/Mozart/.venv` | Python 3.12，sherpa-onnx 1.13.6 |

输入为 `rvc-golden/input/preprocessor-sample-mp4-first-30s.wav`。
格式 16 kHz、单声道、PCM16，时长 30 s，由 `--input` 按实时速度回放。

启动命令：

```bash
.venv/bin/python tools/run_translated_speech.py \
  --backend-config state/translated-speech.yaml \
  --mode captions \
  --input rvc-golden/input/preprocessor-sample-mp4-first-30s.wav \
  --seconds 60 \
  --final-model ~/models/sherpa-onnx/clone-test-downloads/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17 \
  --run-dir ~/mozart-archive/logs/subtitle-20261027/run6 \
  --no-playback --keep-open
```

## 结果

30 秒回放产生 163 条事件、16 个句子。

| 指标 | 数值 |
| --- | ---: |
| 翻译完成 | 14 |
| 翻译被拒 | 2 |
| 翻译耗时中位数 | 360 ms |
| 翻译耗时范围 | 125–5312 ms |
| 识别到定稿的边界 | 16 段 |

验收检查：

- `GET /api/subtitles` 收到连续事件流。
- Vite 代理 `GET /api/subtitles` 同样收到事件流。
- `subtitles.jsonl` 与事件流内容一致。
- `--mode captions` 不启动语音合成服务，参考音色面板显示"连接不可用"。

## 质量问题

### 1. 中文人称被译错

源文「她觉得他妈是事业单位」译成 `He thinks he is a civil servant`。
「她觉得」中的「她」丢失，模型把「他妈」当作主语。

同一段后续事件把「你现在他觉得」译成 `You now feel that he.`。
「他妈是事业单位」译成 `He is a civil servant`。

### 2. 无关词被译成赌博

| 源文 | 译文 |
| --- | --- |
| 你以为你有工作你就不会痛吐过的吗 | You wouldn't be able to gamble without work, right? |
| 你以为你有工作你就不会痛读过的吗 | You think that if you have work, you won't get hurt by gambling? No. |

两次出现 `gamble` 或 `gambling`。源文没有「赌」字。
识别结果是「痛吐」和「痛读」。本次运行没有配置术语表，前端显示"未配置 MOZART_GLOSSARY"。

### 3. 译文尾部多出 `You.`

第 1 句和第 3 句的译文末尾都是 `... do you understand? You.` 与 `... a watermelon. You.`。
下一句源文都以「你」开头。

### 4. 数字表达产生超长字符串

源文「一百倍一千倍一万倍」在两次运行中得到不同结果。

第一次：`Take away ten thousand times more than work, ...` 重复三次，被校验拒绝。

第二次输出一段 `100000000...0`，长度超过 200 字符，耗时 5312 ms，被校验拒绝。

拒绝原因：

```text
Translation rejected: required numerals missing: 100, 1000; unsupported repeated quantities: 10000
```

### 5. 句子切分过碎

一段连续语音被切成 16 段：

```text
你现在他觉得
他妈是事业单位
他是国企两个人都不可以牺牲
这是一种错误的观念对吧
```

主语、谓语、宾语分布在不同段，每段译文都不完整。
同一输入的另一次运行切成 6 段长句，译文更完整。

## 两项结构性发现

### 刷新页面后看不到已有字幕

`api/src/http_api.cpp` 的 `handle_subtitles_stream` 从 `subtitles.jsonl` 末尾开始读取。
浏览器刷新后建立新的 SSE 连接，只收到之后产生的事件。
控制台在字幕产生后打开，面板保持"等待语音输入…"。

### 翻译被拒时字幕没有英文

被拒的句子保留中文，`translation_status` 为 `failed`，`en` 为空字符串。
控制台该行只显示中文。

## 适用范围

- 配置：Jetson Orin Nano Super 8 GB，`~/Mozart/.venv`，`state/translated-speech.yaml`，`--mode captions --no-playback`。
- 样本：`preprocessor-sample-mp4-first-30s.wav` 一个文件，回放 30 s，产生 16 个句子。
- 未验证：真实麦克风输入、`--mode full` 的语音播报、术语表启用后的同音纠错、`--stable-clauses` 与边界模型对切分的影响。阻塞：本轮只测文件回放与字幕通道。解除条件：接入麦克风并启用术语表，重复同一组测量。
- 未验证：翻译耗时在并发与长时间运行下的分布。阻塞：单次 30 s 样本。解除条件：连续运行 10 分钟并统计 p95。
