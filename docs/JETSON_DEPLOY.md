# Jetson Orin Nano Super 8GB · 部署指南

> 将 Project-Mozart 完整管线部署到 NVIDIA Jetson Orin Nano Super 开发者套件
> 目标：实时 AI 变声器，端到端延迟 < 100ms

---

## 1. 硬件规格

| 组件 | Jetson Orin Nano Super | 本机 (参考) |
|------|----------------------|------------|
| GPU | 1024-core Ampere, 67 TOPS | RTX 5070 8GB |
| 显存/统一内存 | 8 GB LPDDR5 (共享) | 8 GB GDDR7 |
| CPU | 6-core ARM Cortex-A78AE | Intel/AMD x86 |
| 存储 | NVMe SSD (推荐) | NVMe SSD |
| 功耗 | 7W ~ 25W | 40W+ (仅 GPU) |
| 价格 | ~$249 | ~$549+ |

---

## 2. 系统初始化

### 2.1 刷写 JetPack 6.x

```bash
# 宿主机 (Ubuntu 22.04)
wget https://developer.nvidia.com/downloads/.../jetson-orin-nano-sd-card-image.zip
# 使用 Balena Etcher 或 dd 刷写至 SD 卡

# 或使用 SDK Manager 刷写至 NVMe
sdkmanager --cli install \
  --target-os Linux \
  --target-hw Jetson_Orin_Nano_DevKit \
  --target "Jetson Orin Nano Super 8GB"
```

### 2.2 首次启动配置

```bash
# SSH 登录 (默认用户 nvidia, 密码 nvidia)
ssh nvidia@<jetson-ip>

# 基础设置
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.10 python3.10-venv python3.10-dev \
    git cmake build-essential libsndfile1-dev \
    portaudio19-dev libasound2-dev

# 配置 swap (8GB 显存对 RVC 模型临界)
sudo systemctl disable nvzramconfig
# 创建 8GB swapfile
sudo fallocate -l 8G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 性能模式
sudo nvpmodel -m 0          # MAXN 模式, 全核全速
sudo jetson_clocks           # 锁最高频率
```

### 2.3 验证 GPU 环境

```bash
# 应显示 Orin GPU + 8GB
tegrastats

# PyTorch 验证
python3 -c "import torch; print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0))"
```

---

## 3. 预处理管线 (preprocessor/)

### 3.1 编译

```bash
cd ~/Project-Mozart/preprocessor

# 编译 RNNoise + Mozart 预处理
make -j6

# 验证
./build/bin/mozart_pre_example --help
```

### 3.2 PipeWire 配置

```bash
# Jetson JetPack 6.x 自带 PipeWire
sudo apt install -y pipewire pipewire-pulse wireplumber

# 创建虚拟 sink (RVC 输出目标)
pactl load-module module-null-sink \
    sink_name=mozart_sink \
    sink_properties=device.description=Mozart_Output

# 创建虚拟 source (其他 app 可当麦克风选)
pactl load-module module-remap-source \
    master=mozart_sink.monitor \
    source_name=Mozart_Voice \
    source_properties=device.description=Mozart_Voice_Changer
```

### 3.3 测试预处理

```bash
# 播放测试音频 → 预处理 → 输出到虚拟设备
./build/bin/mozart_pre_example \
    --input clean_speech.wav \
    --output pipewire \
    --sink mozart_sink
```

---

## 4. RVC 后端部署

### 4.1 方案选择

| 方案 | 模型格式 | 推理引擎 | 优缺点 |
|------|---------|---------|--------|
| **A: Python RVC** | `.pth` | PyTorch (CUDA) | 兼容性最好，部署简单，显存占用 ~3GB |
| **B: C++ ONNX** | `.onnx` | ONNX Runtime (TensorRT) | 延迟最低，需先导出 ONNX |
| **C: C++ libtorch** | `.pth` | libtorch (CUDA) | 与 PyTorch 兼容，编译复杂 |

> **推荐方案 A** 用于快速验证，方案 B 用于生产部署。

### 4.2 方案 A: Python RVC 部署

```bash
# 创建虚拟环境
python3.10 -m venv ~/rvc-venv
source ~/rvc-venv/bin/activate

# PyTorch for Jetson (JetPack 6 预装)
# 已自带，无需额外安装

# 安装依赖
git clone https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI.git
cd Retrieval-based-Voice-Conversion-WebUI
pip install -r requirements.txt

# fairseq (可能需要源码编译)
pip install fairseq 2>/dev/null || {
    git clone https://github.com/One-sixth/fairseq.git
    cd fairseq && pip install -e . && cd ..
}

# 下载基础模型 + 音色模型 (参考 LOCAL_RVC_SETUP.md)
export HF_ENDPOINT=https://hf-mirror.com
python -c "
from huggingface_hub import hf_hub_download
r = 'lj1995/VoiceConversionWebUI'
hf_hub_download(r, 'hubert_base.pt', local_dir='./assets/hubert')
hf_hub_download(r, 'rmvpe.pt', local_dir='./assets/rmvpe')
# ... 完整列表见 LOCAL_RVC_SETUP.md
"
```

### 4.3 方案 B: ONNX + TensorRT (推荐生产)

```bash
# 在 PC 上导出 ONNX 模型
cd Retrieval-based-Voice-Conversion-WebUI
python tools/export_onnx.py \
    --model assets/weights/de_narrator.pth \
    --output de_narrator.onnx

# 拷贝到 Jetson
scp de_narrator.onnx nvidia@jetson:~/models/

# 在 Jetson 上编译 TensorRT Engine
/usr/src/tensorrt/bin/trtexec \
    --onnx=de_narrator.onnx \
    --saveEngine=de_narrator.trt \
    --fp16 \
    --workspace=2048

# 编译 rvc-backend (C++ 端)
cd ~/Project-Mozart/rvc-backend
mkdir build && cd build
cmake .. \
    -DCMAKE_BUILD_TYPE=Release \
    -DONNX_RUNTIME_PATH=/usr/lib/aarch64-linux-gnu \
    -DTENSORRT_PATH=/usr/src/tensorrt
make -j6
```

### 4.4 配置 rvc-backend

```yaml
# rvc-backend/config.yaml
server:
  udp_port: 18000
  http_port: 18080

rvc:
  mock_mode: false
  model_path: /home/nvidia/models/de_narrator.trt
  hubert_path: /home/nvidia/models/hubert_base.onnx
  rmvpe_path: /home/nvidia/models/rmvpe.onnx
  index_path: /home/nvidia/models/de_narrator.index
  device: cuda
  index_rate: 0.75
  f0_method: rmvpe
  filter_radius: 3

audio:
  input_rate: 16000
  output_rate: 48000
```

### 4.5 启动后端

```bash
# 方法 1: 单独启动 rvc-backend
cd ~/Project-Mozart/rvc-backend/build
./rvc_backend config.yaml &

# 方法 2: systemd 服务 (开机自启)
cat << 'EOF' | sudo tee /etc/systemd/system/mozart-rvc.service
[Unit]
Description=Mozart RVC Backend
After=network.target pipewire.service

[Service]
Type=simple
User=nvidia
WorkingDirectory=/home/nvidia/Project-Mozart/rvc-backend/build
ExecStart=/home/nvidia/Project-Mozart/rvc-backend/build/rvc_backend /home/nvidia/Project-Mozart/rvc-backend/config.yaml
Restart=always
RestartSec=5
Environment="HF_ENDPOINT=https://hf-mirror.com"

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now mozart-rvc
```

---

## 5. 完整管线启动

### 5.1 一键启动脚本

```bash
#!/bin/bash
# start_mozart.sh — Project-Mozart 完整管线

echo "=== Mozart Pipeline Start ==="

# 1. 启动 PipeWire 虚拟设备
echo "[1/3] PipeWire virtual devices..."
pactl load-module module-null-sink sink_name=mozart_sink
pactl load-module module-remap-source master=mozart_sink.monitor source_name=Mozart_Voice

# 2. 启动 RVC 后端
echo "[2/3] RVC Backend..."
sudo systemctl start mozart-rvc
sleep 3
curl -s http://localhost:18080/health | grep -q "ok" && echo "  RVC backend: OK" || echo "  RVC backend: FAIL"

# 3. 启动预处理
echo "[3/3] Preprocessor..."
~/Project-Mozart/preprocessor/build/bin/mozart_pre_example \
    --input default \
    --output pipewire \
    --sink mozart_sink &

echo "=== Mozart Pipeline Ready ==="
echo "  Input:  系统默认麦克风"
echo "  Output: 'Mozart_Voice' (虚拟麦克风)"
echo "  Models:  http://localhost:18080/models"
```

### 5.2 验证管线

```bash
# 健康检查
curl http://localhost:18080/health

# 查看当前模型
curl http://localhost:18080/models

# 热切换模型
curl -X POST http://localhost:18080/models/de_narrator/activate

# 查看性能统计
curl http://localhost:18080/status
```

---

## 6. 性能预估

### 6.1 Jetson vs PC 对比

| 阶段 | Jetson Orin Nano (预估) | RTX 5070 PC (实测) |
|------|------------------------|-------------------|
| RNNoise 去噪 | < 1ms | — |
| 降采样 48k→16k | < 1ms | < 1ms |
| HuBERT 特征 | ~3ms / 帧 | 0.77s / 10s |
| RMVPE F0 | ~5ms / 帧 | 1.06s / 10s |
| Generator 推理 | ~8ms / 帧 | 0.49s / 10s |
| FAISS 检索 | ~2ms / 帧 | 即时 |
| **20ms 帧总延迟** | **~20ms** | **—** |

### 6.2 显存预算 (8 GB)

| 组件 | 占用 |
|------|------|
| 系统/桌面 | ~2 GB |
| HuBERT 模型 | ~0.5 GB |
| RMVPE 模型 | ~0.1 GB |
| RVC Generator | ~0.5 GB |
| FAISS 索引 | ~0.1 GB |
| CUDA 运行时 | ~0.5 GB |
| **剩余** | **~4.3 GB** ✅ |

### 6.3 优化方向

- **TensorRT + FP16**：推理延迟再降 40%
- **CUDA Graph**：消除 kernel launch 开销
- **Pipeline 并行**：预处理/推理/后处理流水线化
- **零拷贝 DMA**：预处理→RVC 共享内存直通

---

## 7. 故障排查

### 7.1 GPU 内存不足

```bash
# 检查显存
tegrastats | head -1

# 释放 GPU 缓存
sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'

# 降级为 CPU 推理 (应急)
# config.yaml: device: cpu
```

### 7.2 实时音频卡顿

```bash
# 提升 PipeWire 优先级
sudo sed -i 's/.*nice-level.*/nice-level = -11/' \
    /etc/pipewire/pipewire.conf

# 调大 UDP 缓冲区
# config.yaml: server.udp_buffer_size: 65536

# 锁 CPU 频率
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
```

### 7.3 模型加载失败

```bash
# 检查文件完整性
md5sum models/de_narrator.pth
# 应与下载源一致

# 尝试 CPU 加载验证
python -c "
import torch
ckpt = torch.load('models/de_narrator.pth', map_location='cpu')
print('version:', ckpt.get('version'), 'sr:', ckpt.get('sr'))
"
```

---

## 8. 参考

- [LOCAL_RVC_SETUP.md](LOCAL_RVC_SETUP.md) — 本机 Python RVC 搭建
- [RVC_BACKEND.md](RVC_BACKEND.md) — C++ RVC 后端开发指南
- [PREPROCESSING.md](PREPROCESSING.md) — 预处理管线开发
- [ARCHITECTURE.md](ARCHITECTURE.md) — 系统架构
- [NVIDIA Jetson Orin Nano DevKit Guide](https://developer.nvidia.com/embedded/learn/jetson-orin-nano-devkit-user-guide)
- [RVC Project](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI)
