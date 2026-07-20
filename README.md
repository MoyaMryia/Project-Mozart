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
| `rvc-backend/` | ✅ **RVC 变声后端**：AudioWorker 编排 + ONNX 推理 + HTTP 管理 |
| `tools/` | 🔧 **ONNX 导出脚本**：HuBERT / RMVPE / Generator 一键导出 |
| `state_manager/` | 运行模式与 IO/模型资源编排设计 |
| `docs/` | 📚 **全部文档** |
| `reference/` | 🔧 ZYNQ BTB 硬件参考原理图 |
| `rvc_post_bridge.py` | 🔌 **PC 端适配器**：本地 Python RVC 验证用 |

---

## 文档索引

| 文档 | 内容 | 受众 |
|------|------|------|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构总览 | 所有人 |
| [ONNX_EXPORT_GUIDE.md](docs/ONNX_EXPORT_GUIDE.md) | ⭐ **ONNX 模型导出指南** | PC 端贡献者 |
| [LOCAL_RVC_SETUP.md](docs/LOCAL_RVC_SETUP.md) | PC 端 Python RVC 搭建 | PC 端开发者 |
| [JETSON_DEPLOY.md](docs/JETSON_DEPLOY.md) | ⭐ **Jetson Orin Nano 部署指南** | Jetson 端贡献者 |
| [RVC_BACKEND.md](docs/RVC_BACKEND.md) | C++ RVC 后端开发 | 后端开发者 |
| [PREPROCESSING.md](docs/PREPROCESSING.md) | 预处理管线开发 | 预处理开发者 |
| [FPGA_ROADMAP.md](docs/FPGA_ROADMAP.md) | FPGA 远期规划 | 硬件 |
| [HARDWARE_REFERENCE.md](docs/HARDWARE_REFERENCE.md) | ZYNQ 硬件参考 | 硬件 |

---

## 快速开始

### PC 端：ONNX 模型导出

```bash
cd Retrieval-based-Voice-Conversion-WebUI
# 详见 docs/ONNX_EXPORT_GUIDE.md

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

---

## 当前状态

| 组件 | 状态 |
|------|------|
| preprocessor/ (RNNoise) | ✅ 编译完成，可用 |
| IO/ | ✅ 契约帧 + PipeWire/UDP 驱动 |
| state_manager/ | 🆕 运行模式编排 |
| rvc-backend/ (C++ 骨架) | ✅ CMake + ONNX Runtime 集成完毕 |
| rvc-backend/ (推理组件) | ✅ `onnx_engine` + `feature_extractor` + `inferencer` + `model_loader` 已实现 |
| rvc-backend/ (HTTP API) | ✅ `/health` `/status` `/models` `/activate` 全部可用 |
| ONNX 导出脚本 | ✅ `tools/export_{hubert,rmvpe,generator}_onnx.py` 已就绪 |
| Index 检索 | ✅ FAISS IVF .index 解析 + KNN 检索 |
| Jetson 实机 | 🟢 环境就绪 (JetPack R39)，待 ONNX 模型部署 |
| 音色模型 | 🟡 PC 端已验证 4 个，待导出 ONNX |

---

## 贡献注意事项

- ❌ 不要在 Jetson 上 `pip install fairseq` `pip install torchcrepe`
- ❌ 不要提交 `.pth` 模型文件（用 .onnx）
- ❌ 不要把 build/ 目录提交到 git
- ✅ 新增文档放在 `docs/`，更新本 README 的索引表
- ✅ 新增音色模型 → 先在 PC 导出 ONNX → 再部署 Jetson
