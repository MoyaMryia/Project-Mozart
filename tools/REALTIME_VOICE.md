# 实时变声、字幕与翻译

## 启动和停止

在 Jetson 仓库目录执行命令。

```bash
tools/run-realtime-voice.sh start
tools/run-realtime-voice.sh status
tools/run-realtime-voice.sh stop
```

`start` 启动一个字幕启动器。启动器管理后端、识别和翻译进程。
脚本等待字幕和翻译就绪，再启用 RVC 并启动预处理。
同一个预处理进程将采集音频送到 RVC 和字幕识别。
字幕收到有效音频后，启动命令才返回成功。
启动失败时，脚本回收本次启动的进程并保留日志。

`stop` 先停止预处理，再停止字幕启动器及其子进程。
停止期限由 `STOP_TIMEOUT` 控制。停止超时后，脚本强制回收对应进程树。
脚本通过命令行、进程句柄和创建时间核对停止目标，防止 PID 复用时误停其他进程。
如一条 PID 记录无效，脚本继续停止其他受管进程，并返回错误。
`pre-restart` 只切换设备，保留字幕双发目标。

## 默认配置

| 项目 | 默认值 |
| --- | --- |
| 输入 | HK MIC，`plughw:CARD=MIC,DEV=0` |
| 输出 | USB PnP Sound Device，`plughw:CARD=Device,DEV=0` |
| 后端配置 | `rvc-golden/qiqi-zh-run/backend-index.yaml` |
| 后端 API | `http://127.0.0.1:18181` |
| RVC 音频 | UDP 18101 |
| 字幕音频 | UDP 18100 |
| 翻译服务 | HTTP 18200 |
| Python | 仓库 `.venv/bin/python` |
| 识别 | Zipformer；SenseVoice 复核 |
| 翻译 | Qwen3.5-0.8B，llama-server |

设置 `PYTHON` 可选择其他 Python 环境。
设置 `CONFIG` 和 `API` 时，使配置中的 HTTP 端口与 `API` 一致。
字幕启动器的 `--api-port` 默认仍为 18080，实时脚本按 `API` 传入端口。

设置 `FINAL_MODEL=` 可关闭 SenseVoice 复核，仅使用流式识别。
设置 `GLOSSARY` 和 `CORRECTIONS` 可指定术语表与纠错表。
脚本也读取 `MOZART_GLOSSARY` 和 `MOZART_CORRECTIONS`。

```bash
GLOSSARY="$PWD/rvc-golden/subtitle-20261027/glossary.json" \
CORRECTIONS="$PWD/rvc-golden/subtitle-20261027/corrections.json" \
tools/run-realtime-voice.sh start
```

通过本机前端访问 Jetson 时，将本机 API 代理转发到 Jetson 的 18181 端口。

```bash
ssh -N -L 18080:127.0.0.1:18181 jetson-net
```

## 输出开关

`OUTPUT` 选择扬声器输出哪一路。两路不同时输出。

| 值 | 输出 |
| --- | --- |
| `voice`（默认） | 变声音频；不启动播报 |
| `speech` | 译文播报；后端保持 `idle`，预处理把变声音频写入 `null` |

```bash
OUTPUT=speech REFERENCE=$HOME/models/reference/english.wav \
  tools/run-realtime-voice.sh start
```

| 变量 | 作用 |
| --- | --- |
| `OUTPUT` | `voice` 或 `speech`；其他值在启动前拒绝 |
| `REFERENCE` | 播报模式注册并选中的参考音频，长度 3–15 s |
| `REFERENCE_NAME` | 参考音色的显示名 |
| `SPEECH_EARLY_AUDIO` | 默认 `0`；设为 `1` 时启用提前输出音频 |
| `TTS_PYTHONPATH` | 播报进程使用的独立 Python 运行时目录 |

`OUTPUT=speech` 时，播放设备只由播报进程使用，因此不需要混音配置。
启动返回前，脚本等待字幕状态文件的 `readiness.speech_runtime` 为真。
切换模式需要执行 `stop` 再 `start`。字幕和翻译在两种模式下都保持运行。

在页面停止播报只停止语音会话，字幕和翻译继续。状态文件把语音记为 `degraded.speech`，`voice_session` 变为假。
重新启用会话并准备后，就绪状态恢复。采集开始后，语音运行时不可用只记入 `degraded.speech`，不结束链路。
语音进程退出或报告内部错误仍按失败处理。

## 提前输出音频

脚本默认等待完整片段生成。使用已安装的提前输出运行时后，显式启用该路径。
构建与运行时校验见[提前输出说明](REFERENCE_SPEECH.md#early-audio-output)。

```bash
OUTPUT=speech SPEECH_EARLY_AUDIO=1 \
TTS_PYTHONPATH=/absolute/path/to/early-audio-runtime/python \
REFERENCE=$HOME/models/reference/english.wav \
  tools/run-realtime-voice.sh start
```

服务就绪后，查看 `/api/speech/status` 中的 `runtime.audio_stream_protocol`。
值为 `1` 表示当前运行时启用了提前输出协议。
脚本在启动入口检查指定目录，并将目录转换为绝对路径。
本次本地检查与延迟归因见[调查报告](../reports/speech-latency-20261029/RESULTS.md)。

## 日志与状态

每次启动默认在 `~/mozart-archive/logs/realtime-日期时间-PID/` 保存字幕、分句音频和服务日志。
设置 `CAPTIONS_DIR` 可指定保存目录。
`stack-run-dir` 记录本次目录，`status` 输出该目录。
上次预处理日志和设备状态保存在本次目录的 `previous-runtime/` 下。

`RUN_DIR` 默认是 `/tmp/mozart-realtime`，供设备控制接口读取运行状态。
更改该目录时，同步更改后端配置中的预处理状态路径。
停止操作保留状态文件，并将 PID 文件改名为 `.stopped`。
停止后的预处理日志、设备状态与 PID 记录复制到本次目录的 `runtime-final/`。

## 适用范围

配置见“默认配置”。本入口启动变声、中文字幕和英文翻译。
Jetson 验证包含 2 次完整启动、状态查询和停止；声卡在每次停止后关闭。
主机回归覆盖失败启动、状态损坏、PID 归属和停止超时回收。
