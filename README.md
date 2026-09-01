# Project Mozart

面向 **NVIDIA Jetson Orin Nano Super 8GB** 的实时 AI 变声系统。

插上麦克风说话，板端输出降噪并变声后的实时语音；同时文字路将语音转写、本地翻译并显示字幕。一块边缘板，两条处理路径，运行时零 Python、零 PyTorch 依赖。

---

## 项目定位

做一块"AI 声卡"：插上麦克风就能当声卡用。

- **实时路**：人声降噪 → RVC 变声，端到端延迟约 **2 秒 + 推理 + 60 ms**（受 Generator T=200 固定窗口约束）。
- **文字路**：ASR 转写 → 本地 Qwen(0.8B) 翻译 → 字幕，单句 1–2 秒延迟可接受。
- 两条路径在 8 GB 共享内存板子上分开跑，强互斥单活跃模式。

---

## 架构

```
Microphone/UDP ──► [IO] ──► [Preprocessor C11] ──contract stream──► [RVC Backend C++17] ──► [IO] ──► Speaker/UDP
                          │                                          │
                          │      PC（一次性导出）                      │
                          │  .pth ──export──► .onnx                  │
                          │                                          │
                          ▼                                          ▼
                   PyTorch（PC）                            ONNX Runtime / TensorRT（Jetson）
                   零 Python · 零 PyTorch 依赖
```

**两栖架构**：模型导出在 PC 上一次性完成（PyTorch）；Jetson 仅运行 ONNX Runtime / TensorRT，没有 fairseq、torchcrepe 等 Python 依赖链。

### 两条并行路径

| 路径 | 延迟目标 | 组件 |
|------|---------|------|
| **实时变声** | ~2 s + 推理（Generator T=200 硬约束） | Preprocessor → HuBERT → RMVPE → Generator |
| **翻译字幕** | 1–2 s / 句 | STT（sherpa-onnx）→ LLM（Qwen3.5-0.8B）→ 字幕 |

---

## 项目结构

| 目录 | 语言 | 职责 | 状态 |
|------|------|------|------|
| `IO/` | C++17 + C ABI | 统一契约帧、UDP/PipeWire/Mock 驱动、SPSC 无锁环 | UDP/Mock ✅；PipeWire 物理声卡 ⚠️ stub |
| `preprocessor/` | C11 | ALSA 采集 → HPF → RNNoise 全湿 → 3:1 降采样 → VAD 滞回 → MZRT UDP | ✅ 可用 |
| `rvc-backend/` | C++17 | ONNX/TensorRT 推理、滑动窗口流式管线、HTTP API、模型热切换 | 主链路 ✅；GPU 生产落地待验证 |
| `state/` | C++17 | 顶层守护进程 `mozart_stated`：模式控制器、IDLE/RT_RVC/FILE_RVC 强互斥 | ✅ 已编码 |
| `api/` | C++ | 原生 socket HTTP 服务器：`/health`、`/status`、`/models`、`/file/*`、`/subtitles`（SSE） | ✅ |
| `monitor/` | C++ | 系统遥测：CPU、内存、GPU 负载、PipeWire 状态 | ✅ |
| `frontend/` | Vue 3 + TS + Tailwind | 控制面板：模式切换、模型管理、文件队列、字幕 | 框架 ✅；实时波形/部分按钮未接线 |
| `tools/` | Python | ONNX 导出脚本、STT/TTS 服务、全链 demo、并发基准 | ✅（文字路未接入 C++ 守护进程） |
| `rvc-golden/` | Python | PyTorch Golden 参考，用于 ONNX 回归测试 | ✅ |

---

## 快速开始

### 1. 导出 ONNX 模型（PC 端一次性）

```bash
# 基础模型
python tools/export_hubert_onnx.py
python tools/export_rmvpe_onnx.py

# 音色模型
python tools/export_generator_onnx.py <model>.pth

# 部署到 Jetson
scp *.onnx *.index moyamryia@<jetson-ip>:~/models/
```

### 2. 构建预处理器（Jetson）

```bash
cd preprocessor && make -j6

# 实时模式（仅发送，无本地播放）
./build/bin/mozart-pre -d hw:1,0

# 实时模式（发送 + 本地扬声器播放回包）
./build/bin/mozart-pre -d hw:1,0 -o plughw:1,3

# 离线模式（无需麦克风）
./build/bin/mozart-pre -i input.wav
```

### 3. 构建 RVC 后端（Jetson）

```bash
cd rvc-backend && mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DUSE_ONNX=ON && make -j6
./rvc_backend ../config.yaml
```

**GPU 推理（TensorRT 直载）**：将 `.engine` 文件放到同名 `.onnx` 旁即可自动加载。确认日志出现 `Engine backend: TensorRT (GPU)`。若 TRT 失败回退 ONNX，需源码编译 CUDA 版 ONNX Runtime 后加 `-DUSE_CUDA_EP=ON` 重新构建。

### 4. 运行生产守护进程（推荐）

```bash
cd state && mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release && make -j6
./mozart_stated ../config.yaml
```

---

## 推理管线

```
16 kHz 输入
  → RMVPE ONNX        （mel 128×128 → F0）
  → HuBERT ONNX       （audio → [T, 768] 特征）
  → Index 检索        （FAISS IVF，KNN1，无 FAISS 运行时依赖）
  → Generator ONNX/TRT （feats + pitch + sid → 48 kHz 音频）
  → protect 混合      （与原特征混合，保留原声特点）
```

### 流式架构

`StreamingRvc` 缓冲 2 秒音频（T=200 硬约束），在独立推理线程中运行，输出通过 60 ms 交叉淡化重叠相加。帧序号跳变、段变化、PTS 缺口等不连续事件会触发全量状态重置。

### 实测性能（Jetson Orin Nano Super 8GB）

| 模型 | 输入窗口 | FP32 | FP16 | GPU 占用 |
|------|---------|------|------|---------|
| RMVPE（F0） | mel 128×128 = **1.28 s**，固定 | 20.9 ms | **9.4 ms** | ~0.7 % |
| HuBERT（特征） | audio 3200 = **0.2 s** | 9.2 ms | **4.5 ms** | ~2.3 % |
| Generator（合成） | T=200 = **2 s**，固定 | 89.9 ms | **37.6 ms** | ~1.9 % |

全链合计 ≈ 98 ms / 2 s 音频 ≈ **5 % GPU**。

> 注：以上为实验室 TensorRT FP16 数字。生产构建需确认 TensorRT 引擎或 GPU ONNX Runtime 在 Jetson 上实际加载。

---

## 契约帧

所有组件间通信使用 `IO/include/mozart/frame_meta.h` 中定义的 16 字节元数据头：

```c
typedef struct {
    uint64_t pts_ns;       // 呈现时间戳（纳秒）
    uint32_t frame_idx;    // 单调递增帧号
    uint8_t  vad_flag;     // 0=静音 1=语音
    uint8_t  energy_db;    // 能量 0-255
    uint8_t  conf;         // 降噪置信度 0-255
    uint8_t  segment_id;   // 语音段 ID（0=静音间隔）
} mozart_frame_meta_t;     // 16 字节，static_assert 保证
```

| 阶段 | 采样率 | 帧大小 | 每帧字节 |
|------|--------|--------|---------|
| raw（设备 → 预处理） | 48 kHz | 960 样本（20 ms） | 3856 B |
| input（预处理 → 后端） | 16 kHz | 320 样本（20 ms） | 1296 B |
| output（后端 → 设备） | 48 kHz | 960 样本（20 ms） | 3856 B |

UDP 包格式：MZRT 魔数（`0x4D5A5254`）+ 20 字节头部 + PCM payload。输入包 1300 字节（< MTU 1500）。

---

## HTTP API

原生 socket 实现，零 web 框架依赖。默认端口 18080。

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/status` | GET | 模式、当前模型、延迟统计、bypass 计数 |
| `/monitor` | GET | CPU、内存、GPU 负载、PipeWire 状态 |
| `/logs` | GET | 后端日志环形缓冲 |
| `/models` | GET | 列出可用音色模型 |
| `/models/{id}/activate` | POST | 热切换音色模型（~200 ms） |
| `/mode/switch` | POST | 切换运行模式（IDLE/RT_RVC/FILE_RVC） |
| `/file/convert` | POST | 上传音频进行批量转换 |
| `/file/status` | GET | 任务队列状态 |
| `/subtitles` | GET | SSE 字幕 JSONL 流 |
| `/parameters` | GET/PUT | RVC 推理参数 |
| `/presets` | GET | 已保存参数预设 |

> 注：`rt_zero_shot` / `file_zero_shot` 模式尚未实现，调用返回 HTTP 501。

---

## 配置

`rvc-backend/config.yaml`：

```yaml
rvc:
  models_dir: "./models"
  hubert_path: "./assets/hubert/hubert_base.onnx"
  rmvpe_path: "./assets/rmvpe/rmvpe.onnx"
  f0_method: "rmvpe"
  pitch_shift: 0
  index_rate: 0.75
  protect: 0.33
  device: "cuda"

network:
  audio:
    port: 18000      # UDP 契约流
  control:
    port: 18080      # HTTP API
```

---

## 技术栈

| 层级 | 技术 |
|------|------|
| 音频 I/O | PipeWire / UDP / ALSA，SPSC 无锁环，MZRT 协议 |
| 预处理 | RNNoise（xiph），C11，ARM NEON 优化 |
| 特征提取 | HuBERT ONNX（768-dim），RMVPE ONNX |
| 合成 | Generator ONNX / TensorRT，按音色模型加载 |
| 编排 | C++17 状态机，强互斥 |
| 控制面 | HTTP + SSE，原生 socket |
| 前端 | Vue 3 + Vite + TypeScript + Tailwind CSS |
| 导出工具 | Python（fairseq、torchcrepe） |

---

## 状态机

强互斥——任意时刻仅一个模式活跃（8 GB 共享内存约束）：

| 状态 | 说明 |
|------|------|
| `IDLE` | 不处理，释放资源 |
| `RT_RVC` | 实时变声 |
| `FILE_RVC` | 批量文件转换（队列串行） |

切换规则：实时模式立即中断；文件模式在忙时优雅等待当前任务完成。同大类切换（RT_RVC ↔ FILE_RVC）复用已加载模型（~200 ms）；跨大类切换会卸载并重建实例。

---

## 当前主要缺口

详见 [TODO.md](TODO.md)，核心未竟项：

1. **真模型出声验证**：放置 HuBERT/RMVPE/Generator 模型，跑通 `mozart-pre live` + 后端滑窗全链。
2. **扬声器出声路径**：`mozart-pre -o <alsa设备>` 已实现 UDP 回包接收与 ALSA 播放，待真模型联合验证。
3. **GPU 推理生产落地**：TensorRT 直载或 GPU ONNX Runtime 在 Jetson 上的实际构建与加载验证。
4. **PipeWire 物理声卡驱动**：当前为 stub，需实现 `pw_stream` capture/playback。
5. **文字路接入 C++ 守护进程**：STT/LLM/TTS 当前由独立 Python 进程运行。
6. **零样本变声**：尚未实现。

---

## 贡献

- 请勿在 Jetson 上 `pip install fairseq torchcrepe`
- 请勿提交 `.pth` 模型文件（先导出 ONNX）
- 请勿提交 `build/` 目录
- 新增文档请放在相关模块目录
- 新增音色模型：PC 端导出 ONNX → 部署到 Jetson
- RVC 质量回归：先跑 Golden Model → 对比 ONNX → 再测后端

参见 [AGENTS.md](AGENTS.md) 调试工作流与 [DESIGN.md](DESIGN.md) 系统设计。

---

## 文档索引

| 文档 | 内容 |
|------|------|
| [TODO.md](TODO.md) | 当前任务与实测基准 |
| [DESIGN.md](DESIGN.md) | 系统设计、架构、契约、实现路线 |
| [TARGET.md](TARGET.md) | 产品愿景与交付目标 |
| [AGENTS.md](AGENTS.md) | RVC 调试工作流 |
| [state/README.md](state/README.md) | 状态机与资源编排 |
| [state/API.md](state/API.md) | HTTP API 规范 |
| [frontend/DEPLOYMENT.zh-CN.md](frontend/DEPLOYMENT.zh-CN.md) | 前端构建与部署 |
| [rvc-golden/README.md](rvc-golden/README.md) | Golden 模型回归测试 |

---

## 相关项目

- [RVC WebUI](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) — PC 端导出源
- [RNNoise](https://github.com/xiph/rnnoise) — 降噪上游
- [ONNX Runtime](https://github.com/microsoft/onnxruntime) — 推理引擎
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) — STT/TTS 引擎
