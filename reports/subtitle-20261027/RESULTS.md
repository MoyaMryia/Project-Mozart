# 字幕链路 30 秒实测

日期：2026-10-27。

字幕链路的显示与订阅缺陷已修复，中文改用复核文本。翻译质量仍有 3 类问题。

## 配置

| 环节 | 组件 | 参数 |
| --- | --- | --- |
| 识别 | `sherpa-onnx` zipformer-zh-14M | 流式 |
| 定稿 | SenseVoice zh-en-ja-ko-yue int8 | 句子结束 |
| 翻译 | `llama-server` + qwen35-0.8b-text-Q4_K_M | CUDA，batch 256，cache 128 MiB |
| 后端 | `mozart_stated` | `state/translated-speech.yaml`，RVC 关闭，端口 18080 |
| 字幕桥 | `tools/subtitle_bridge.py` | `--delivery-policy coverage` |
| 术语表 | `rvc-golden/subtitle-20261027/glossary.json` | 12 条借款与赌博术语 |
| 纠错表 | `rvc-golden/subtitle-20261027/corrections.json` | 6 条识别纠错 |
| 运行环境 | `~/Mozart/.venv` | Python 3.12，sherpa-onnx 1.13.6 |

术语表与纠错表是本地配置，按 `.gitignore` 第 121 行不入库。内容如下。

`glossary.json`：

```json
{
  "催收": "debt collection",
  "借条": "IOU note",
  "戒赌": "quit gambling",
  "庄家": "the house",
  "征信": "credit record",
  "额度": "credit limit",
  "网贷": "online lending",
  "网赌": "online gambling",
  "赌徒": "gambler",
  "逾期": "overdue",
  "套路贷": "predatory lending",
  "高利贷": "loan sharking"
}
```

`corrections.json`：

```json
{
  "痛赌": "赌博",
  "痛吐": "赌博",
  "痛读": "赌博",
  "王独": "网赌",
  "姐姐": "戒戒",
  "痛赌不的": "碰赌博了"
}
```

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
  --glossary rvc-golden/subtitle-20261027/glossary.json \
  --corrections rvc-golden/subtitle-20261027/corrections.json \
  --run-dir ~/mozart-archive/logs/subtitle-20261027/run13 \
  --no-playback --keep-open
```

## 本次修复

### 中文改用复核文本

流式识别把「赌博」写成「痛赌」「痛吐」「痛读」。复核模型给出「痛赌」，纠正表再映射到「赌博」。
控制台此前显示流式原文，现在是复核文本，并在下一行标出「原始识别：…」。

```
译文源优先级：纠错后的复核文本 → 流式原文（复核不可信时）
```

### 纠错结果被覆盖（已修）

`subtitle_bridge.py` 在写入 `refined_text` 之后，又用未纠错的原文覆盖了 `refined_zh`。
纠错表因此对显示与译文都不生效。现在两者都取纠错后的文本。

### 复核文本的边界回声（已修）

复核模型会把下一句的开头带进本句末尾：

```
流式原文：她觉得他妈是事业单位懂吗
复核文本：她觉得他妈是事业单位，懂吗？你。
```

规则：本句已用标点收住，后面再跟不超过两个字的片段，且流式原文没有这个片段时删除。
流式原文含该片段时保留原样，因为无法判断它是本句内容还是回声。

### 连续终结标点（已修）

复核偶尔给出「懂吗？。」。连续终结标点只保留第一个。

### 刷新页面后没有历史字幕（已修）

`GET /api/subtitles` 此前从文件末尾读取，新连接收不到已有事件。
现在连接时先补发最近 200 行，再转入增量读取。控制台刷新后立即显示上下文。

### 术语表未配置（已修）

`run_translated_speech.py` 新增 `--glossary` 与 `--corrections`。
两个路径写入子进程环境，后端、字幕桥、识别服务读同一个文件。
控制台的术语表面板此前显示「读取术语表失败：未配置 MOZART_GLOSSARY」，现在可读写。

同时修复环境变量被覆盖：翻译服务启动前的 `env = os.environ.copy()` 重建了字典，
导致字幕桥收不到术语表路径。

## 结果

30 秒回放产生 161 条事件、16 个句子。

| 指标 | 数值 |
| --- | ---: |
| 翻译完成 | 14 |
| 翻译被拒 | 2 |
| 翻译耗时中位数 | 347 ms |
| 翻译耗时范围 | 125–5312 ms |

修复前后的对比：

| 项目 | 修复前 | 修复后 |
| --- | --- | --- |
| 中文「赌博」 | 痛赌 / 痛吐 / 痛读 | 碰赌博了 |
| 译文 | `you won't gamble`（上下文错） | `You wouldn't gamble if you had a job.` |
| 句尾回声 | `…懂吗？你。` → `… do you understand? You.` | `…懂吗？` |
| 重复标点 | `懂吗？。` | `懂吗？` |
| 刷新后字幕 | 空白，显示「等待语音输入…」 | 立即显示最近 200 行 |
| 术语表面板 | 读取失败 | 可读写 |

具体两段：

```text
中文：以为你你有工作，你就不会碰赌博了吗？不可能的，像你这种就是。
译文：You wouldn't gamble if you had a job. Impossible, you're like that.

中文：说说好听一点，就是捡捡了芝麻丢了西瓜。
译文：Tell me a bit better. It means picking up a sesame seed and throwing away a watermelon.
```

验收检查：

- `GET /api/subtitles` 与 Vite 代理都收到连续事件流。
- 新连接立即收到 161 条已有事件。
- `GET /api/glossary` 返回两个路径下的术语表。
- `--mode captions` 不启动语音合成服务，参考音色面板显示「连接不可用」。

## 未修复的问题

### 中文仍有识别错误

复核文本自身不准确，纠错表只能按整串替换：

| 复核文本 | 实际内容 |
| --- | --- |
| 以为你你有工作 | 你以为你有工作 |
| 借走远远比工作重要 | 借走钱远远比工作重要 |
| 帮你借读之间相比 | 帮你借钱之间相比 |

重复字与错字需要人工核对。纠错表按整串匹配，只覆盖已确认的条目。

### 数字表达译文超长

「一百倍一千倍一万倍」在两次运行中分别得到重复短句与超过 200 字符的数字串。
两次都被数字校验拒绝，字幕只保留中文。校验拦截生效，译文不出现。

### 句子切分过碎

同一段语音被切成 16 段：

```text
你现。
人的像你这。
这种就是。
工作跟。
```

同一输入的早期运行切成 6 段长句。切分依赖语音停顿，本次未启用 `--stable-clauses`。

## 适用范围

- 配置：Jetson Orin Nano Super 8 GB，`~/Mozart/.venv`，`state/translated-speech.yaml`，`--mode captions --no-playback --keep-open`。
- 样本：`preprocessor-sample-mp4-first-30s.wav` 一个文件，回放 30 s，产生 16 个句子。
- 未验证：真实麦克风输入、`--mode full` 的语音播报、`--stable-clauses` 与边界模型对切分的影响。阻塞：本轮只测文件回放与字幕通道。解除条件：接入麦克风并启用边界模型，重复同一组测量。
- 未验证：翻译耗时在并发与长时间运行下的分布。阻塞：单次 30 s 样本。解除条件：连续运行 10 分钟并统计 p95。
