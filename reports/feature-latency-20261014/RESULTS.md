# 逐功能延迟实测（2026-10-14）

本次测量覆盖当前部署的软件功能、常用控制接口及执行中的取消操作。原生服务使用提交 `531a18d` 的 Release 二进制。生产代码未改动。

实时 RVC 的软件首输出等待为 400 ms，单块推理中位耗时为 64.19 ms。8.399 s 文件使用现有 T398 TensorRT Generator 时，热转换中位耗时为 7.00 s。连续译文播报的主要等待发生在播放队列。

## 配置与计时方法

- 平台：Jetson Orin Nano；Linux 6.8.12-1021-tegra；`MAXN_SUPER`。未改变电源配置，未固定频率，未清空磁盘缓存。
- 实时 RVC：七七 v2、48 kHz、F0、768 维特征；TensorRT 特征与分离 Generator；240 ms 推理块、2.5 s 历史、400 ms 播放等待预算。
- 文件 RVC：同一七七检查点的已存在 T398 资产；HuBERT 与 Generator 使用 TensorRT。长输入 RMVPE 使用 ONNX CPU，状态记录给出了形状不匹配回退原因。
- 识别：Zipformer 14M，CPU，2 个线程。可选精修使用 SenseVoice int8，CPU，2 个线程。
- 翻译：Qwen 0.8B Q4_K_M；现有 CUDA 构建的 llama-server；`-ngl 99 -c 2048 -b 256 -ub 128 -np 1 -t 2 -tb 2`。
- 参考音色播报：PocketTTS int8，CPU，2 个线程，5 步，种子 42；独立早期音频运行时；参考音频使用已有七七录音。
- 同步调用使用单调时钟。HTTP 从建立请求前计时，到完整正文读取结束。接口测量使用 Jetson 本机回环，排除 SSH 传输时间。
- 文件完成状态每 20 ms 轮询一次。完成观测值额外包含约 0–20 ms 的轮询等待及请求、调度时间。
- 播放使用按音频时长等待的 ALSA `null`。时间戳描述软件管道，不代表扬声器出声。
- P95 使用样本线性插值。单样本不列 P95；少量重复只描述本次样本，不代表长期尾部延迟。

RVC、文件和降噪复用固定 espeak 输入。识别使用已有中文录音的固定 8.399 s 截取。语音链使用同一份 120 s 处理后录音。输入摘要见 [metrics.json](metrics.json)，实际二进制和资产摘要见 [manifest.json](manifest.json)。

## 音频与文本功能

| 功能 | 计时起止点及范围 | 样本数 | 中位数 | P95 |
| --- | --- | ---: | ---: | ---: |
| 降噪、VAD、高通及重采样 | 每个 20 ms 帧调用 mozart_dsp_process；文件读取不计入 | 1260 | 5.57 ms | 5.61 ms |
| 关闭降噪的预处理 | 同上；VAD 使用能量门限 | 1260 | 0.03 ms | 0.04 ms |
| 实时 RVC 块推理 | inferencer 入口→输出完成；含 3 s 排空；不含模型构造 | 47 | 64.19 ms | 65.18 ms |
| 实时 RVC 首输出 | 首个输入帧→首个输出音频帧；两次独立回放均为 400 ms | 2 | 400.00 ms | 400.00 ms |
| 实时 RVC UDP 往返 | 契约包发送→对应编号回包；不等于缓冲音频内容延迟 | 570 | 0.27 ms | 0.86 ms |
| 旁路 UDP 往返 | 同上；仅重采样，不运行 RVC | 570 | 0.25 ms | 0.88 ms |
| 静音 UDP 往返 | 同上；输出静音，不运行 RVC | 570 | 0.22 ms | 0.72 ms |
| 在线识别首字 | 固定录音首帧开始→首个非空识别文本 | 3 | 746.52 ms | 746.64 ms |
| 在线识别定稿 | 固定录音最后采样位置→最后非空定稿；包含断句静音 | 3 | 666.86 ms | 667.21 ms |
| 在线识别实际解码 | 每次 decode_stream；排除未触发解码的帧 | 96 | 25.72 ms | 26.77 ms |
| 可选识别精修 | SenseVoice 完整处理固定 8.399 s 录音 | 5 | 641.08 ms | 652.38 ms |
| 句界分类 | 固定 30 字文本→边界检查返回 | 20 | 6.32 ms | 6.59 ms |
| 隔离翻译热请求 | 请求开始→完成翻译检查；3 段固定文本，排除首个请求 | 8 | 393.49 ms | 480.06 ms |
| 语音链中的翻译 | 每个已提交片段的翻译开始→检查结束；包含重试与失败候选 | 41 | 398.00 ms | 740.00 ms |
| 源字幕到客户端 | ASR 事件发出→Python SSE 客户端收到正文 | 292 | 113.53 ms | 200.60 ms |
| 参考音色完整预览 | 提交→完整 WAV 首次就绪；含首次推理 | 3 | 3229.19 ms | 3438.27 ms |
| 空队列参考音色首播 | 提交→首个音频块开始 null 播放；单任务串行 | 3 | 922.01 ms | 922.96 ms |
| 空队列参考音色完整生成 | 提交→全部音频块生成结束 | 3 | 3249.61 ms | 3255.21 ms |
| 连续译文首播 | 每个父字幕最终 ASR 发出→该父字幕首个音频块播放；含队列 | 23 | 8152.46 ms | 11066.91 ms |

预览首次就绪为 3461.50 ms，后两次为 3229.19 ms 和 3227.61 ms。每次输出均为 4.40 s。空队列首播为约 922 ms，不能代替连续播报队列的等待时间。

在线识别的首帧时间不等于人工标注的第一音节。定稿测试使用截取文件的末尾，随后输入 2 s 静音。三次产生相同识别文本，但本次未做字错率验收。

首次合成输入的三次在线识别均未产生文本。该负向测试已保留。该输入的解码耗时和精修结果不计入有效录音识别表。

## 文件转换与内部阶段

| 路径 | 计时范围 | 首次 | 热运行样本数 | 热运行中位数 | 热运行最大值 |
| --- | --- | ---: | ---: | ---: | ---: |
| T398 TensorRT Generator | 上传开始→完成状态可见；8.398875 s 输入 | 11.292 s | 2 | 7.003 s | 7.041 s |
| T398 ONNX CPU 对照 | 同上；测试包装没有相邻 engine | 110.475 s | 4 | 106.001 s | 106.026 s |

首次文件请求发生在模型激活以后。首次计时包含按需加载的质量特征引擎，排除守护进程启动和前面的模式切换。CPU 对照不代表 TensorRT 文件路径。

| 内部阶段 | 计时范围 | 样本数 | 中位数 | P95 |
| --- | --- | ---: | ---: | ---: |
| 实时 RMVPE | 4960 个 16 kHz 采样的 mel、引擎及解码 | 47 | 37.62 ms | 38.64 ms |
| 实时 HuBERT | 44800 个采样的特征引擎 | 47 | 24.52 ms | 29.60 ms |
| 实时 Generator | 准备结束后→Generator 输出完成 | 47 | 24.57 ms | 25.16 ms |
| 文件 RMVPE | 含上下文的 166382 个采样；含首次准备开销 | 3 | 1951.77 ms | 2381.18 ms |
| 文件 HuBERT | 每个 64000 采样窗口的特征引擎 | 30 | 58.78 ms | 59.39 ms |
| 文件 Generator | 每个 T398 窗口；含输入准备 | 30 | 419.62 ms | 421.39 ms |

实时 HuBERT 与 RMVPE 并行，不能将两项中位数相加。文件每次处理 10 个重叠窗口。Generator 的重复计算与约 1.9 s 的热 RMVPE CPU 处理均影响文件总耗时。

## 冷启动与模型切换

这里的冷启动指新进程或未缓存模型，不表示冷磁盘缓存。

| 功能 | 计时范围 | 本次结果 | 样本数 |
| --- | --- | ---: | ---: |
| 原生服务，含实时资产 | 进程启动→首次 status 响应；20 ms 轮询 | 4.608 s | 1 |
| Zipformer 初始化 | 构造识别器→返回 | 1.660 s | 1 |
| SenseVoice 初始化 | 构造识别器→返回 | 2.317 s | 1 |
| 句界模型初始化 | 模型读取、会话构造及校验→返回 | 54.44 ms | 1 |
| 翻译服务启动 | 进程启动→health 成功；20 ms 轮询 | 3.173 s | 1 |
| 翻译首次请求 | 模型就绪后首个文本请求→检查结束 | 393.13 ms | 1 |
| 参考音色服务 HTTP | 进程启动→status 可访问；20 ms 轮询 | 168.85 ms | 1 |
| 参考音色模型就绪 | 进程启动→status 包含 runtime；20 ms 轮询 | 1.567 s | 1 |

## 原生控制接口

下表使用 TensorRT 文件配置的独立进程。无并行业务负载。`file status` 行在文件处理期间采集。每次计时包含连接和完整响应读取。

| 接口或操作 | 样本数 | 中位数 | P95 |
| --- | ---: | ---: | ---: |
| `GET health` | 20 | 0.40 ms | 1.08 ms |
| `GET logs` | 20 | 0.81 ms | 1.36 ms |
| `GET models` | 20 | 0.56 ms | 0.68 ms |
| `GET monitor` | 20 | 1.12 ms | 1.50 ms |
| `GET parameters` | 20 | 0.62 ms | 1.38 ms |
| `GET presets` | 20 | 0.49 ms | 0.95 ms |
| `GET status` | 20 | 0.47 ms | 0.66 ms |
| `PUT parameters` | 20 | 0.67 ms | 0.94 ms |
| `activate quality model and file mode` | 1 | 841.75 ms | — |
| `cached model activate` | 20 | 1.00 ms | 1.66 ms |
| `cancel queued file` | 1 | 0.51 ms | — |
| `clear finished files` | 20 | 0.57 ms | 0.87 ms |
| `clear logs` | 20 | 0.47 ms | 0.74 ms |
| `delete cancelled file` | 1 | 1.12 ms | — |
| `delete completed file` | 3 | 2.07 ms | 2.39 ms |
| `delete preset` | 10 | 0.82 ms | 1.29 ms |
| `download file result` | 3 | 3.70 ms | 3.79 ms |
| `file status` | 1202 | 0.89 ms | 1.27 ms |
| `idle already idle` | 1 | 0.88 ms | — |
| `pause file queue` | 20 | 0.88 ms | 1.17 ms |
| `reset parameters` | 20 | 0.52 ms | 1.32 ms |
| `resume file queue` | 20 | 0.40 ms | 0.79 ms |
| `routing` | 20 | 0.51 ms | 1.10 ms |
| `save preset` | 10 | 0.83 ms | 1.25 ms |
| `start realtime worker cached` | 10 | 1.64 ms | 2.73 ms |
| `stop realtime worker` | 10 | 102.01 ms | 105.40 ms |
| `uncached model activate` | 1 | 725.69 ms | — |
| `upload and enqueue file` | 4 | 1.96 ms | 2.21 ms |

模型缓存切换只证明两个模型标识之间的缓存命中耗时。两者使用同一七七资产。该结果不代表任意两个人声模型的加载时间。

暂停文件队列的测量发生在空闲时。该操作不会中断正在运行的推理。参数更新测量表示控制请求完成，不表示新参数已经出现在扬声器音频中。

## 参考音色接口与任务停止

此处参考音色接口直接访问独立服务。随后单独列出原生代理测量。

| 接口或操作 | 样本数 | 中位数 | P95 |
| --- | ---: | ---: | ---: |
| `GET completed speech job` | 60 | 1.34 ms | 1.83 ms |
| `GET speech/jobs` | 20 | 1.12 ms | 1.36 ms |
| `GET speech/session` | 20 | 0.97 ms | 1.13 ms |
| `GET speech/status` | 20 | 1.32 ms | 1.55 ms |
| `GET voices` | 20 | 1.02 ms | 1.38 ms |
| `delete reference voice` | 1 | 2.32 ms | — |
| `download early playback` | 3 | 2.76 ms | 2.97 ms |
| `download preview` | 3 | 1.95 ms | 2.13 ms |
| `speech session update` | 20 | 1.15 ms | 1.35 ms |
| `speech stop idle` | 20 | 1.35 ms | 1.85 ms |
| `submit early playback` | 3 | 5.54 ms | 6.41 ms |
| `submit preview` | 3 | 4.54 ms | 5.87 ms |
| `upload reference voice` | 1 | 130.85 ms | — |
| `active file cancel acceptance` | 1 | 1.79 ms | — |
| `active speech cancel` | 1 | 37.79 ms | — |
| `active speech stop` | 1 | 3072.67 ms | — |
| `direct speech status` | 20 | 1.34 ms | 1.96 ms |
| `pending mode switch acceptance` | 1 | 0.93 ms | — |
| `proxied speech status` | 20 | 1.71 ms | 1.98 ms |
| `queued speech cancel` | 1 | 4.89 ms | — |

文件取消在第一个 T398 Generator 窗口返回后发出。取消接口耗时 1.79 ms。任务在 4.295 s 后进入 `cancelled`，模式在 4.296 s 后成为 `idle`。当前实现等待整个 RVC 调用返回。

正在合成的播报取消耗时 37.79 ms。正在播放的播报停止耗时 3.073 s。两项仅各测 1 次。后者与当前进程终止最长 3 s 等待相符；尚未通过系统调用追踪确定等待发生在哪个子进程。

## 连续语音链与排队

120 s 固定录音独立回放；RVC 关闭。句界分类启用。识别、翻译和参考音色播报同时运行。

| 阶段 | 样本数 | 中位数 | P95 |
| --- | ---: | ---: | ---: |
| `asr event to bridge receive` | 292 | 0.55 ms | 38.71 ms |
| `bridge source record to SSE observed` | 292 | 105.68 ms | 196.78 ms |
| `translation submit to result` | 41 | 402.94 ms | 810.93 ms |
| `translation wait and dispatch overhead` | 41 | 7.30 ms | 11.30 ms |
| `speech queue` | 41 | 969.76 ms | 3792.36 ms |
| `speech start to first ready` | 41 | 931.26 ms | 1417.41 ms |
| `speech ready to first playback` | 41 | 6204.55 ms | 8016.44 ms |
| `speech submit to first playback` | 41 | 8490.25 ms | 11043.76 ms |
| `parent final ASR to first playback` | 23 | 8152.46 ms | 11066.91 ms |

各行使用不同样本范围，中位数不能直接相加。父字幕可能在定稿前已有片段播放，因此个别“定稿到首播”样本可以为负。

23/23 个父字幕覆盖源文本范围，41/41 个播报任务完成。22/23 个父字幕通过现有翻译检查；第 5 个保留了包含中文“大妈”的失败候选。完成率不代表识别或翻译语义正确率。

生成音频合计 123.60 s，超过 120 s 输入。97 个父字幕内部片段间隔中，1 个超过 20 ms，最大为 21.32 ms。本次测试不能证明长时间播放队列延迟稳定。

ASR 事件到字幕客户端的中位耗时为 113.53 ms，P95 为 200.60 ms。代码确认 SSE 文件读取线程每次休眠 200 ms。该轮询是字幕延迟的一个明确改进点；本次没有改变轮询行为。

## 输出核对与适用边界

- 两个原生回放均为 47 个实时推理块。启动后无欠载、迟块、推理错误或输入输出溢出。旁路和静音各收发 570 帧。
- 去掉 400 ms 启动输出后，两次实时回放均与此前已验证的后端 PCM 完全一致，共比较 402240 个采样。
- 每种文件配置的重复输出完全一致。CPU 与 GPU 输出均为 403146 个采样；相对均方根误差为 0.00004824，余弦相似度为 0.99999999884。
- 六个参考音色任务均完成。各任务的完整 WAV 与有序音频块拼接完全一致，所有采样均为有限值。
- 本次用于延迟与执行路径核对。原始 PyTorch、ONNX 和实时后端的已有质量证据见 [效率报告](../efficiency-20261014/RESULTS.md)。本次不新增语义或听感通过声明。
- USB 声卡采集、数模转换、扬声器、虚拟 USB 主机及浏览器绘制均未进行物理闭环计时。已有 USB 声卡不等于已接好回环线路。
- `rt_zero_shot` 与 `file_zero_shot` 当前返回 HTTP 501，没有推理延迟可测。PipeWire 硬件路由和其他未部署引擎不在本次测量范围。
- VAD 进入与退出的 3 帧、25 帧门限来自代码，分别对应 60 ms、500 ms 的条件累计。该值不是本次实测的声学检测延迟。

## 复测与证据

原始计时、输入和输出保存在 `rvc-golden/feature-latency-20261014/`。该目录忽略大型生成文件。小型测量摘要已纳入 Git。

- [逐样本汇总和统计](metrics.json)
- [资产摘要与最后清理记录](manifest.json)
- [原生测量脚本](../../rvc-golden/benchmark_feature_latency.py)
- [识别与参考音色脚本](../../rvc-golden/benchmark_speech_features.py)
- [取消测量脚本](../../rvc-golden/benchmark_cancel_latency.py)
- [预处理计时工具](../../rvc-golden/benchmark_dsp_latency.c)
- [字幕及队列分析脚本](../../rvc-golden/analyze_feature_latency.py)
- [汇总及音频检查脚本](../../rvc-golden/collect_feature_latency.py)

复测命令及模型路径可从各测试目录的 `backend.json`、`results.json` 和语音链的 `protocol.json` 读取。使用隔离目录。测试会创建任务和音色配置，并在结束时关闭所启动的进程。

```bash
python3 rvc-golden/analyze_feature_latency.py \
  rvc-golden/feature-latency-20261014/feature-latency-speech-20261014 \
  rvc-golden/feature-latency-20261014/speech-analysis.json
python3 rvc-golden/collect_feature_latency.py \
  rvc-golden/feature-latency-20261014 reports/feature-latency-20261014/metrics.json \
  --realtime-reference rvc-golden/runtime-efficiency-20261014/campaign-final/parallel-1/qiqi-espeak-parallel-mozart-backend.wav
```

汇总脚本要求 Python、NumPy 与 FFmpeg。Jetson 测量环境还需要 soundfile、sherpa-onnx 和现有模型资产。预处理计时工具链接已测 Release 库，不重新实现降噪函数。
