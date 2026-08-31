# Project Mozart · Realtime AI Voice Changer

面向 **NVIDIA Jetson Orin Nano Super 8GB** 的实时 AI 变声器。

---

## ⚠️ 两栖架构（Contributor 必读）

本项目分 PC 端和 Jetson 端，**不要把 PyTorch 模型直接丢到 Jetson 上**：

```
[麦克风/UDP] ──► [IO] ──► [预处理 C11] ──契约流──► [RVC 后处理 C++17] ──► [IO] ──► [扬声器/UDP]

                      │                              │
                      │      PC 端 (一次性导出)        │
                      │  .pth ──export──► .onnx      │
                      │  只用一次，用完即弃            │
                      │                              │
                      ▼                              ▼
              PyTorch (PC)                ONNX Runtime (Jetson)
              零 Python · 零 PyTorch 依赖
```

> **为什么：** Jetson 的 PyTorch 是 NVIDIA 定制版，跟 RVC 依赖链（fairseq/torchcrepe/gradio）不兼容。
> Jetson 只吃 ONNX——JetPack 自带 ONNX Runtime + TensorRT 原生加速。

---

## 项目结构

| 目录 | 说明 |
|------|------|
| `IO/` | 统一契约帧、PipeWire/UDP 驱动、SPSC 环和 C-ABI 生命周期 |
| `preprocessor/` | ✅ **预处理管线**：RNNoise 去噪 + 降采样 → 16kHz 契约流 (C11) |
| `rvc-backend/` | ✅ **RVC 变声后端**：AudioWorker 编排 + ONNX/TensorRT 推理 + HTTP 管理 |
| `api/` | ✅ **HTTP API**：控制面、模型管理、文件转换、字幕流 |
| `monitor/` | ✅ **监控**：延迟统计、VAD 状态、系统快照 |
| `state/` | ✅ 顶层 daemon：运行模式、IO/模型资源和 worker 生命周期编排 |
| `rvc-golden/` | 🔧 **Golden Model 对比**：PyTorch 基线、ONNX 中间张量、回归测试 |
| `frontend/` | 🌐 **控制台**：Vue 3 + Tailwind 管理界面 |
| `rvc_post_bridge.py` | 🔌 **PC 端适配器**：本地 Python RVC 验证用 |

---

## 文档索引

| 文档 | 内容 | 受众 |
|------|------|------|
| [state/README.md](state/README.md) | 系统架构、状态机、契约帧、双通道调度 | 所有人 |
| [state/API.md](state/API.md) | HTTP API 完整规范 | 前端/集成开发者 |
| [jetson_deploy_prompt.txt](jetson_deploy_prompt.txt) | ⭐ **Jetson Orin Nano 部署指南** | Jetson 端贡献者 |
| [jetson_remaining_tasks.txt](jetson_remaining_tasks.txt) | 当前 Jetson 待办 | 部署人员 |
| [rvc-golden/README.md](rvc-golden/README.md) | Golden Model 调试工作流 | RVC 质量调试者 |
| [rvc-backend/RVC_BACKEND.md](rvc-backend/RVC_BACKEND.md) | C++ RVC 后端开发 | 后端开发者 |
| [preprocessor/README.md](preprocessor/README.md) | 预处理管线开发 | 预处理开发者 |
| [frontend/DEPLOYMENT.md](frontend/DEPLOYMENT.md) | 前端构建部署 | 前端开发者 |

---

## 快速开始

### PC 端：ONNX 模型导出

```bash
cd Retrieval-based-Voice-Conversion-WebUI
# 详见 rvc-golden/README.md 的导出章节

# 导出基础模型 (一次)
python tools/export_hubert_onnx.py
python tools/export_rmvpe_onnx.py

# 每个音色模型各跑一次
python tools/export_generator_onnx.py de_narrator.pth

# 部署到 Jetson
scp -P 6001 *.onnx *.index moyamryia@<jetson-ip>:~/models/
```

### Jetson 端：预处理

```bash
cd ~/Mozart/preprocessor && make -j6
./build/bin/mozart_pre_example --input clean_speech.wav
```

### Jetson 端：RVC 后端

```bash
cd ~/Mozart/rvc-backend && mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DUSE_ONNX=ON && make -j6
vim ../config.yaml  # mock_mode: false
./rvc_backend ../config.yaml
```

### Jetson 端：顶层 Daemon (推荐生产用)

```bash
cd ~/Mozart/state && mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release && make -j6
./mozart_stated ../config.yaml
```

---

## 当前状态

| 组件 | 状态 |
|------|------|
| preprocessor/ (RNNoise) | ✅ 编译完成，可用 |
| IO/ (PipeWire + UDP + SPSC) | ✅ 契约帧 + 零拷贝环形缓冲 |
| state/ (mozart_stated) | ✅ `IDLE` / `RT_RVC` / `FILE_RVC` 强互斥编排 |
| rvc-backend/ (C++ 骨架) | ✅ CMake + ONNX Runtime + TensorRT 集成完毕 |
| rvc-backend/ (推理组件) | ✅ `onnx_engine` + `feature_extractor` + `inferencer` + `model_loader` 已实现 |
| rvc-backend/ (HTTP API) | ✅ `/health` `/status` `/models` `/activate` `/file/*` `/subtitles` 全部可用 |
| monitor/ | ✅ 延迟直方图、VAD 快照、SSE 流 |
| ONNX 导出脚本 (PC 端) | ✅ `tools/export_{hubert,rmvpe,generator}_onnx.py` 已就绪 |
| Index 检索 | ✅ FAISS IVF .index 解析 + KNN 检索 |
| Jetson 实机 | 🟢 环境就绪 (JetPack R39)，REAL 模式跑通 |
| 音色模型 | 🟡 de_narrator 已加载，待导出更多 ONNX |
| 前端控制台 | 🟡 Vue 3 基础框架就绪，待接入 API |

---

## 核心技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 音频 I/O | PipeWire / UDP | C-ABI 契约帧，SPSC 无锁环，MZRT 协议 |
| 预处理 | RNNoise (xiph) | C11，NEON 优化，外部权重 blob |
| 特征提取 | HuBERT (ONNX) | RVC v2 最终层 768 维，TensorRT 可选 |
| 音高估计 | RMVPE (ONNX) | Mel + F0 + salience，TensorRT 可选 |
| 生成器 | Generator (ONNX) | 每音色一模型，支持 .index 检索 |
| 编排 | C++17 | 双通道 CPU/GPU 调度，强互斥状态机 |
| 控制面 | HTTP + SSE | 原生 socket 实现，无第三方 Web 框架 |
| 前端 | Vue 3 + Tailwind | Vite 构建，TypeScript |

---

## 贡献注意事项

- ❌ 不要在 Jetson 上 `pip install fairseq` `pip install torchcrepe`
- ❌ 不要提交 `.pth` 模型文件（用 .onnx）
- ❌ 不要把 build/ 目录提交到 git
- ✅ 新增文档放在对应模块目录下，更新本 README 的索引表
- ✅ 新增音色模型 → 先在 PC 导出 ONNX → 再部署 Jetson
- ✅ RVC 质量回归：先跑 Golden Model → 对比 ONNX → 再进后端

---

## 相关仓库

- [RVC WebUI (PC 端导出源)](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI)
- [RNNoise (预处理上游)](https://github.com/xiph/rnnoise)
- [ONNX Runtime (推理引擎)](https://github.com/microsoft/onnxruntime)