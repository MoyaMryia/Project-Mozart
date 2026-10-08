# Project Mozart

Project Mozart 是面向 NVIDIA Jetson Orin Nano Super 8GB 的语音处理系统。
系统提供实时 RVC 变声、翻译字幕、文件转换和参考音色播报。
RVC 使用 C/C++ 与 ONNX Runtime 或 TensorRT；字幕和参考播报使用独立 Python 进程。
所选部署链路无需 PyTorch，模型导出和 Golden 回归使用 PyTorch。

## 当前状态

| 功能 | 已实现行为 | 验证范围与限制 |
| --- | --- | --- |
| 实时 RVC | ALSA 预处理客户端与 UDP 后端 | `qiqi-zh-realtime` 的 TensorRT 配置已验证，首帧约 320 ms |
| 文件 RVC | 上传、串行队列、取消请求、结果下载 | 已验证 HTTP 全链；长文件的内存与取消时延仍需限制 |
| 翻译字幕 | Zipformer 转写、可选 SenseVoice 校正、Qwen 翻译、SSE 字幕 | 已接通；人称、领域词和跨句语义仍有错误 |
| 参考音色播报 | PocketTTS 逐片段合成、播放、预览和下载 | 已接通；数量检查不能证明语义或声线正确 |
| PipeWire 设备接口 | 占位实现 | 当前物理音频使用 `mozart-pre` 的 ALSA 路径 |
| 零样本语音转换 | 预留模式 | `rt_zero_shot` / `file_zero_shot` 返回 HTTP 501 |

`320 ms` 来自已验证配置的首帧输入至首帧输出。稳态推理块耗时中位数为 `88 ms`，p95 为 `93 ms`。
这些数字不是任意模型、动态 ONNX 或全部并发组件的性能保证。
详细证据见 [Golden 参考](rvc-golden/README.md) 和 [报告索引](reports/README.md)。

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

确认构建日志找到所需推理运行时。缺少 ONNX Runtime 时，当前 CMake 会退回 `USE_ONNX=OFF`。
这种构建可用于主机测试，不能作为真实 ONNX 推理部署通过的证据。
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

`device: cuda` 不能证明实际使用 GPU。同名 `.engine` 加载成功时优先 TensorRT；
ONNX CUDA EP 需要相应运行时与 `-DUSE_CUDA_EP=ON`。检查实际加载日志。
生产当前不预加载 `.index`，设置 `index_rate` 也不会启用缺失的索引。

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
split-only 模型不能代替普通文件 Generator。

显式选择模型后，日志应出现 `upstream realtime (240ms block + 2.5s past + SOLA)`。
`2.5 s` 是滚动历史上下文，不是额外等待时间。

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
  -DUSE_ONNX=OFF -DUSE_TENSORRT=OFF -DBUILD_TESTS=ON
cmake --build build/host -j2
ctest --test-dir build/host --output-on-failure
python3 -m unittest discover -s tools/tests -v
npm --prefix frontend run build
```

TCP/UDP 测试需要本地 socket 权限。主机测试通过不能替代 Golden、ONNX 张量对比或板上试听。
RVC 回归顺序见 [AGENTS.md](AGENTS.md) 和 [Golden 参考](rvc-golden/README.md)。

## 文档

- [TARGET.md](TARGET.md)：产品目标与验收范围。
- [DESIGN.md](DESIGN.md)：当前架构与数据契约。
- [TODO.md](TODO.md)：剩余问题、优先级和验收条件。
- [state/API.md](state/API.md)：当前 HTTP 接口。
- [docs/WRITING.md](docs/WRITING.md)：英文 STE 与中文编写约定。
- [reports/README.md](reports/README.md)：历史报告和最新审计。
