# Project Mozart · Realtime AI Voice Changer

面向 **NVIDIA Jetson Orin Nano 8GB** 的实时 AI 变声器。

```
真实麦克风 ──► [预处理 preprocessor/ (C11)] ──契约流──► [RVC 后处理 rvc-backend/ (C++17)] ──► 扬声器
```

| 目录 | 说明 |
|------|------|
| `preprocessor/` | 预处理管线：RNNoise 去噪 + 降采样 → 16kHz 契约流 |
| `rvc-backend/` | RVC 变声后端：UDP 网络契约 + libtorch 推理 |
| `docs/` | 📚 **全部文档（已合并）** |
| `reference/` | ZYNQ BTB 硬件参考原理图 |

**快速入口：**
- [系统架构](docs/ARCHITECTURE.md)
- [预处理开发指南](docs/PREPROCESSING.md)
- [RVC 后端开发指南](docs/RVC_BACKEND.md)
- [FPGA 远期规划](docs/FPGA_ROADMAP.md)
- [ZYNQ 硬件参考](docs/HARDWARE_REFERENCE.md)

### 快速构建

```bash
# 预处理
cd preprocessor && make && make run

# RVC 后端
cd rvc-backend && mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release && make -j$(nproc)
./rvc_backend
```

当前 Phase 1：预处理 RNNoise 就绪，后端为 mock 直通。更多详见 `docs/`。
