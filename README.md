# Mozart

Mozart 是面向 NVIDIA Jetson Orin Nano Super 8GB 的语音处理系统。
系统提供实时 RVC 变声、翻译字幕、文件转换和参考音色播报。
RVC 使用 C/C++ 与 ONNX Runtime 或 TensorRT；字幕和参考播报使用独立 Python 进程。
所选部署链路无需 PyTorch，模型导出和 Golden 回归使用 PyTorch。

## 当前状态

| 功能 | 已实现行为 | 验证范围与限制 |
| --- | --- | --- |
| 实时 RVC | ALSA 预处理客户端与 UDP 后端 | `qiqi-zh-realtime` 已通过 400 ms 启动预算下的 120 s 组合检查 |
| 文件 RVC | 上传、串行队列、取消请求、结果下载 | 输入最长 300 s；FFmpeg 可取消；整段推理仍需等待返回 |
| 翻译字幕 | Zipformer 转写、可选 SenseVoice 校正、Qwen 翻译、SSE 字幕 | 已接通；人称、领域词和跨句语义仍有错误 |
| 参考音色播报 | PocketTTS 逐片段合成、播放、预览和下载 | 已接通；数量检查覆盖数额，语义与声线由听测确认 |
| PipeWire 设备接口 | 占位实现 | 当前物理音频使用 `mozart-pre` 的 ALSA 路径 |
| 零样本语音转换 | 预留模式，演示不接入 | `rt_zero_shot` / `file_zero_shot` 返回 HTTP 501；MeanVC2 评估后内存不足，见[评估报告](reports/meanvc2-eval-20261021/RESULTS.md) |
| USB 音频桥 | 两端 `snd-aloop` 加 FunctionFS bulk，全双工 | 30 分钟受压与拔插、重启恢复已验，见[报告](reports/usb-audio-20261020/RESULTS.md) |
| 术语表与同音纠错 | 可配置词表，拼音匹配与词频判真词 | 字幕与译文同源纠正，见[说明](usb-audio/README.md) |

首帧输入至首帧输出的早期实测为 `320 ms`，稳态推理块中位 `88 ms`、p95 `93 ms`。
数字对应固定输入与固定模型，其他配置需要重新测量。证据见 [Golden 参考](rvc-golden/README.md)。

近期实测：

| 主题 | 结论 | 报告 |
| --- | --- | --- |
| 效率优化 | GPU 张量直连、特征并行、按需加载 | [效率验证](reports/efficiency-20261014/RESULTS.md) |
| 逐功能延迟 | 变声、识别、翻译、字幕、播报的计时 | [逐功能延迟](reports/feature-latency-20261014/RESULTS.md) |
| 播放积压 | 待播量峰值 47.6 s → 23.2 s | [播放积压](reports/backlog-fix-20261019/RESULTS.md) |
| 变速上限 | 1.15 → 1.50 倍，按需选择比例 | [变速上限](reports/tempo-cap-20261020/RESULTS.md) |
| 翻译模型 | 0.8B 与 2B、4B 通过率相同，保留 0.8B | [模型对比](reports/translation-model-20261021/RESULTS.md) |
| USB 音频桥 | 全双工，30 分钟受压无丢帧 | [USB 报告](reports/usb-audio-20261020/RESULTS.md) |

时长控制见[译文时长参考](docs/TRANSLATION_DURATION.md)与[灵活变速说明](docs/FLEXIBLE_TEMPO.md)。

## 数据流

```text
麦克风 / WAV -> mozart-pre (ALSA、降噪、降采样)
                    -> UDP -> mozart_stated -> RVC -> UDP -> mozart-pre -> 扬声器
                    -> UDP -> ASR -> 翻译 -> 字幕 JSONL -> SSE -> 前端
                                       -> 参考音色 TTS -> ALSA 播放
```

`mozart_stated` 管理 RVC 模式和文件队列。
`tools/run_translated_speech.py` 管理字幕与参考播报所需的子进程，包含原生后端。
实时 RVC 和文件 RVC 互斥；字幕与参考播报可独立运行。组件共存的内存预算需要按具体配置实测。

## 目录

| 目录 | 职责 |
| --- | --- |
| `IO/` | 契约帧、UDP、Mock、SPSC 环和 PipeWire 占位接口 |
| `preprocessor/` | C11 预处理核心、ALSA 采集播放和定速 WAV 回放 |
| `rvc-backend/` | RVC 推理、模型管理、实时和文件工作线程 |
| `state/` | 后端入口 `mozart_stated` 和组件生命周期 |
| `api/` | 原生 HTTP API 与字幕 SSE |
| `monitor/` | CPU、内存、GPU 和 PipeWire 状态采集 |
| `frontend/` | Vue 3 控制界面 |
| `tools/` | 模型导出、ASR、翻译、参考播报和启动器 |
| `rvc-golden/` | 可复用参考输入、输出、张量和验证脚本 |
| `reports/` | 带日期的实验与审计记录 |
| `usb-gadget/` | USB 输出决策与已关闭路线的调查证据 |

## 构建

在仓库根目录执行。生产构建需要 CMake、C++17 编译器、ALSA、yaml-cpp、nlohmann/json、spdlog 和 FFmpeg。
真实推理还需要 ONNX Runtime 或相应 TensorRT 资产及开发文件。
根构建会查找已安装的 nlohmann/json 和 spdlog；其他依赖行为见组件 CMake 文件。

```bash
cmake -S . -B build-gpu -DCMAKE_BUILD_TYPE=Release -DUSE_ONNX=ON
cmake --build build-gpu -j4
./build-gpu/state/mozart_stated rvc-backend/config.yaml
```

当 `USE_ONNX=ON` 时，缺少 ONNX Runtime 头文件或库会使配置失败。
主机测试或纯 TensorRT 部署必须显式设置 `USE_ONNX=OFF`。无 ONNX 运行时的构建会拒绝加载 ONNX 资产。
独立构建 `rvc-backend/` 不产生 `mozart_stated`。

前端使用已经安装的 Node.js 与 npm：

```bash
npm --prefix frontend ci
npm --prefix frontend run build
npm --prefix frontend run dev
```

开发界面默认位于 `http://127.0.0.1:5173/`，通过 Vite 将 `/api` 转发到端口 18080。
需要 nvm 时，先执行 `source ~/.nvm/nvm.sh`。
完整部署步骤见 [中文](frontend/DEPLOYMENT.zh-CN.md) / [English](frontend/DEPLOYMENT.md)。

### Python 依赖

管线使用 `tools/` 下的 Python 脚本，运行环境需要 `numpy`、`soundfile`、`sherpa-onnx`、`pyusb`。

术语表启用后额外需要两项，未配置术语表时不加载：

```bash
pip install pypinyin jieba
```

`pypinyin` 生成同音索引，`jieba` 提供词频，用于区分真词与识别错字。
词表与纠错表格式见 [state/glossary.example.json](state/glossary.example.json) 与 [state/corrections.example.json](state/corrections.example.json)。

## 首次文件转换

普通模型目录结构：

```text
rvc-backend/models/<model_id>/
├── <model_id>.onnx
├── <model_id>.engine       # 可选，同名引擎加载成功时优先使用
└── config.json
```

在 `rvc-backend/config.yaml` 指定特征资产。相对文件路径以配置文件目录为基准。

```yaml
rvc:
  models_dir: "./models"
  hubert_path: "./assets/hubert/hubert_base.onnx"
  rmvpe_path: "./assets/rmvpe/rmvpe.onnx"
  realtime_hubert_path: ""
  realtime_rmvpe_path: ""
  device: "cuda"
  index_rate: 0.0
  mock:
    generator: false
    hubert: false
    rmvpe: false
storage:
  file_rnnoise: false
```

`device` 只写入模型加载日志。运行后端由同名 `.engine` 资产与编译选项决定。
同名 `.engine` 存在且加载成功时，后端使用 TensorRT；加载失败时回退 ONNX Runtime，原因记入 `*_fallback_reason`。
ONNX CUDA EP 在 `-DUSE_CUDA_EP=ON` 且 ONNX Runtime 提供 CUDA EP 时附加。
`/api/status` 的 `model.*_backend` 报告实际后端。
生产不预加载 `.index`。索引未加载时 `index_rate` 不参与计算，`model.has_index` 为 `false`。

先查询状态，再显式选择目标模型：

```bash
curl http://127.0.0.1:18080/api/health
curl http://127.0.0.1:18080/api/models
curl http://127.0.0.1:18080/api/status
curl -X POST http://127.0.0.1:18080/api/mode/switch \
  -H 'Content-Type: application/json' \
  --data '{"mode":"file_rvc","model_id":"<model_id>"}'
curl -X POST http://127.0.0.1:18080/api/file/convert \
  -F 'audio_file=@input.wav' -F 'model_id=<model_id>'
```

使用返回的 `job_id` 查询 `/api/file/status?job_id=...`。
任务完成后下载 `/api/file/result?job_id=...`。
`/api/health` 只报告进程存活，不验证模型、GPU、音质或麦克风。

## 低延迟实时配置

当前已验证模型为 `qiqi-zh-realtime`。它使用额外的固定形状资产：

| 资产 | 输入 | 输出 |
| --- | --- | --- |
| split Generator front | feats `[1,280,768]` 等条件 | z `[1,192,30]` |
| split Generator decoder | z `[1,192,30]` 等条件 | audio `[1,1,14400]` |
| realtime HuBERT | audio `[1,44800]` | 特征 |
| realtime RMVPE | mel `[1,128,32]` | F0 |

配置中的 `realtime_hubert_path` 和 `realtime_rmvpe_path` 指向对应 `.onnx`，
旁边保留同名 `.engine`。普通 `hubert_path` / `rmvpe_path` 继续供文件和 quality 路径使用。
文件与 quality 路径只加载 `<model_id>.onnx`。仅配置 split 资产时，文件路径不可用。

显式选择模型后，日志应出现 `upstream realtime (240ms block + 2.5s past + SOLA)`。
`2.5 s` 是滚动历史上下文，不是额外等待时间。

实时 TensorRT 路径默认直接连接 Generator front 与 decoder 的 GPU 张量。
HuBERT 与 RMVPE 默认重叠执行。CPU／ONNX 路径保留主机张量连接。
对照测试可设置 `MOZART_RVC_DEVICE_CHAIN=0` 或 `MOZART_RVC_PARALLEL_FEATURES=0`，分别关闭对应优化。
设置 `MOZART_RVC_PROFILE=1` 可记录分段耗时。并行模式下，日志字段 `hubert_wait_prepare` 记录等待耗时，HuBERT 计算耗时见 `[profile][hubert]` 行的 `engine` 字段。

`rvc.realtime_playout_delay_ms` 默认设为 `400`，表示实时输出的最小启动等待预算，单位为 ms。
计量从首个输入契约帧开始。该预算覆盖静音转有声及组合负载下的计算抖动，不修改音频样本。
允许值为 `0` 至 `1000`，按 `20` 递增。单独运行 RVC 时可设为 `0`，关闭此预算并测量欠载。
较慢的首块仍可能使实际启动等待超过预算。该配置只用于 upstream realtime 路径。
状态中的 `stream.playout_delay_ms` 报告配置预算，`stream.queued_output_ms` 报告当前输出缓冲时长。

配置两个 realtime 特征路径后，普通 quality 特征引擎在首次使用时加载。
模型管理器默认保留两个缓存对象；正在使用的模型继续保留，暂时允许超过此数量。
切换模型后，管理器回收多余的闲置对象。该数量限制不是整个进程的内存上限。

`/api/status` 的 `model` 对象报告各阶段的 `*_backend`、`*_asset` 和 `*_fallback_reason`。
`onnx_cuda_ep` 记录会话附加的 CUDA 执行提供程序；`tensorrt` 记录加载的 TensorRT 引擎。
前端的“推理运行状态”可展开查看这些信息。

```bash
python3 tools/stream_audio_udp.py input.wav realtime-output.wav \
  --backend 127.0.0.1:18000 --api http://127.0.0.1:18080 \
  --model-id qiqi-zh-realtime --flush-seconds 3 \
  --trim-leading-underruns --trim-to-rvc-duration
```

物理采集播放可使用预处理客户端。先用 `arecord -L` 和 `aplay -L` 确认稳定设备名称。

```bash
./build-gpu/preprocessor/mozart-pre \
  -d plughw:CARD=Device,DEV=0 -o plughw:CARD=Device,DEV=0
```

如果当前构建的程序位于组件输出目录，以构建结果为准。
模式切换只停止后端 UDP 工作线程，不停止外部 ALSA 采集进程。
电脑连接方案见 [USB 决策](usb-gadget/DECISION.md)。

## 翻译与参考音色播报

使用仅播报配置可避免加载未使用的 RVC 引擎。以下命令依赖已准备好的 Python 环境和模型：

```bash
.venv/bin/python tools/run_translated_speech.py \
  --backend-config state/translated-speech.yaml \
  --reference reference.wav --input test-audio.mp4 --seconds 120
```

省略 `--input` 可使用麦克风。启动器不会启动前端服务器或可选 RVC 监听客户端。
参考 WAV、模型路径、会话接口和队列上限见 [REFERENCE_SPEECH.md](tools/REFERENCE_SPEECH.md)。
字幕保留原文。数字、否定和部分边界检查只能发现已覆盖的错误，不能证明翻译正确。

## 验证

主机测试无需模型或 GPU：

```bash
cmake -S rvc-backend -B build/host -DCMAKE_BUILD_TYPE=Debug \
  -DUSE_ONNX=OFF -DUSE_TENSORRT=OFF -DBUILD_TESTS=ON -DMOZART_BUILD_TESTS=ON
cmake --build build/host -j2
ctest --test-dir build/host --output-on-failure
python3 -m unittest discover -s tools/tests -v
npm --prefix frontend run build
```

TCP/UDP 测试需要本地 socket 权限。主机测试通过不能替代 Golden、ONNX 张量对比或板上试听。
RVC 回归顺序见 [AGENTS.md](AGENTS.md) 和 [Golden 参考](rvc-golden/README.md)。

## 文档

- [docs/README.md](docs/README.md)：文档导航，从这里进入其余文档。
- [TARGET.md](TARGET.md)：产品目标与验收范围。
- [DESIGN.md](DESIGN.md)：当前架构与数据契约。
- [TODO.md](TODO.md)：剩余问题、优先级和验收条件。
- [state/API.md](state/API.md)：当前 HTTP 接口。
- [docs/WRITING.md](docs/WRITING.md)：英文 STE 与中文编写约定。
- [reports/README.md](reports/README.md)：历史报告和最新审计。
