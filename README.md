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

### PyTorch 环境

当前开发设备为 NVIDIA Jetson Orin Nano 8GB（ARM64、L4T 39.2.0、系统 CUDA 13.2），项目使用 Python 3.12 虚拟环境 `.venv`。

重新创建环境并安装经过验证的 PyTorch 组合：

```bash
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install --index-url https://pypi.org/simple \
  'torch==2.11.0' 'nvpl-lapack==0.4.0.1' numpy
```

验证 GPU：

```bash
python -c "import torch; x=torch.randn((1024,1024),device='cuda'); y=x@x; torch.cuda.synchronize(); print(torch.__version__, torch.cuda.get_device_name(0), y.device)"
```

已验证配置：PyTorch `2.11.0+cu130`、NumPy `2.5.1`、cuDNN `9.19`，CUDA 张量和矩阵乘法可在 Orin GPU 上运行。PyTorch 目前会提示官方架构列表未显式包含 Orin 的 `sm_87`；该警告不影响上述已验证的 CUDA、cuBLAS 和 cuDNN 操作。

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
