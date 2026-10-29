# 播报投递解耦与语义反例验收

本次修改把播报 HTTP 投递从翻译线程移到独立的持久队列。
本地阻塞测试中，第一条播报请求未返回时，第二条译文和播报队列记录均已生成。
整句与稳定短句两种路径通过同一项检查。

## 播报队列

- 翻译完成后立即发布英文。投递状态单独使用 `speech_submission_status`。
- 队列保存请求、原始标识、字幕快照、投递状态、次数和错误。
- 断网和可重试 HTTP 错误按有界策略重试。策略定义见[独立播报投递](../../tools/REFERENCE_SPEECH.md#independent-speech-delivery)。
- 关闭超时后保留未确认请求。迟到的响应更新持久状态，并在下一次启动时补发状态通知。
- 已写入播报队列的翻译恢复时使用原快照，避免再次生成不同译文。
- 内部程序错误终止工作线程并向主线程传播。存储失败保留原翻译任务，等待重启恢复。

服务端的持久去重账本保存已受理标识。
任务清理前保存终态快照，随后重复投递返回原任务 ID，不重新合成。
旧版本任务在清理时补写账本，启动时无需逐项扫描全部任务来建立去重表。

显式停止更新停止时间，并通过错误计数报告持久化结果。
停止前产生但延迟到达的请求，即使会话重新启用，也会被拒绝。
停止状态成功写入后，该时间在语音服务重启后保留。存储错误的处理流程见[投递说明](../../tools/REFERENCE_SPEECH.md#independent-speech-delivery)。

## 语义验收

新增固定样本的显式参考契约，检查主体、关系、对象、数量、否定和开放句式。
契约包含 8 条日志来源样本和 6 条独立保留样本。
保留样本不进入生产词表。
参考契约按来源中文的事实关系显式编写。

以下改义译文会失败：

| 来源 | 反例 |
| --- | --- |
| 原神是一款游戏 | Genshin Impact is a movie. |
| 原神是一款游戏 | Genshin Impact is not a game. |
| 英国拒绝了我们的条款 | We rejected the United Kingdom's terms. |
| 我麻了兄弟 | I am stunned by your brother. |
| 我们买了三台打印机 | We bought four printers. |
| 而我们要做的是 | We shall do what we must do |

检查接受已标注的同义表达，完整性断言不依赖句末标点。
旧关键词条件保留，语义条件作为额外失败条件。
历史结果中的第 9 条被新验收拒绝，原始 `results.json` 保持不变。

## 本地复现

```bash
python3 -m unittest tools.tests.test_speech_outbox tools.tests.test_speech_admissions
python3 -m unittest tools.tests.test_translation_semantics tools.tests.test_refinement_selection
python3 rvc-golden/asr-selection-20261028/probe.py \
  --recorded-results rvc-golden/asr-selection-20261028/results.json
```

最后一条命令返回 `1`。第 9 条语义检查失败，6 条保留样本因缺少实测输出而逐项失败。
该模式只检查保存的文本，不启动模型。
独立模型回放模式会另外翻译 6 条保留样本，并把结果纳入同一失败判定。

## 适用范围

- 配置：本地 Python 测试、模拟 HTTP 响应和 fake 语音工作进程；前端使用 Node 测试和构建检查。
- 样本：44 条历史结果，其中 8 条有语义契约；另有 6 条独立保留样本，以及本地投递、恢复和停止故障测试。
- 语义断言用于固定样本离线验收。生产翻译流程继续执行已有的文本与识别审核。
- 6 条保留样本的实际模型输出、真实播放去重和端到端性能尚未测量。阻塞：Jetson 关机。解除条件：开机后运行独立模型回放及相同录音的整链路测试。
