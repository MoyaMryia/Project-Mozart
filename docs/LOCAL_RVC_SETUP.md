# Python RVC 后端 · 本地搭建指南

> 将 Python RVC (Retrieval-based Voice Conversion) WebUI 接入 Project-Mozart 作为后处理引擎
> 已验证平台：Windows 11 + RTX 5070 Laptop 8GB

---

## 1. 架构定位

```
Project-Mozart                              本地 Python RVC
┌──────────────────┐   16kHz/f32/20ms       ┌─────────────────────┐
│ preprocessor/    │ ────────────────────►  │ RVC WebUI           │
│ (C11, RNNoise)   │   契约流音频            │ (infer-web.py)      │
│                  │                        │ · HuBERT 特征提取   │
│ clean_speech.wav │ ◄────────────────────  │ · RMVPE 音高检测    │
│ (48kHz→16kHz)    │   48kHz 变声输出       │ · VITS 神经声码器   │
└──────────────────┘                        │ · FAISS 特征检索    │
                                            └─────────────────────┘
```

---

## 2. 环境准备

### 2.1 硬件要求

| 组件 | 最低 | 推荐 |
|------|------|------|
| GPU | NVIDIA GTX 1060 6GB | RTX 3060+ 8GB |
| 显存 | 4 GB | 8 GB |
| 内存 | 8 GB | 16 GB |
| 磁盘 | 10 GB | 20 GB (模型缓存) |
| Python | 3.10 ~ 3.11 | 3.11 |

### 2.2 克隆仓库

```powershell
git clone https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI.git
cd Retrieval-based-Voice-Conversion-WebUI
```

### 2.3 创建虚拟环境

```powershell
python -m venv venv
venv\Scripts\activate
```

> **注意：** 推荐 Python 3.11，Python 3.12+ 部分依赖不兼容。

### 2.4 安装 PyTorch (CUDA)

```powershell
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
```

### 2.5 安装依赖

```powershell
# 使用 Python 3.11 专用依赖清单
pip install -r requirements-py311.txt
```

> **Windows 注意事项：**
> - `fairseq` 在 Windows 上编译困难，推荐从 [One-sixth/fairseq](https://github.com/One-sixth/fairseq) 克隆源码后 `pip install -e .` 安装
> - `pyworld` 需要 MSVC C++ 编译工具，也可安装预编译版本 `pip install pyworld --only-binary pyworld`
> - 国内用户设置镜像：`$env:HF_ENDPOINT="https://hf-mirror.com"`

---

## 3. 模型下载

### 3.1 基础模型（必需）

```python
import os
os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'
from huggingface_hub import hf_hub_download

repo = 'lj1995/VoiceConversionWebUI'

# HuBERT 特征提取
hf_hub_download(repo, 'hubert_base.pt', local_dir='./assets/hubert')

# RMVPE 音高检测
hf_hub_download(repo, 'rmvpe.pt', local_dir='./assets/rmvpe')

# 预训练声码器 (v1 + v2)
for ver in ['pretrained', 'pretrained_v2']:
    for m in ['D32k','D40k','D48k','G32k','G40k','G48k',
              'f0D32k','f0D40k','f0D48k','f0G32k','f0G40k','f0G48k']:
        hf_hub_download(repo, f'{ver}/{m}.pth', local_dir=f'./assets/{ver}')

# UVR5 人声分离 (可选)
for m in ['HP2_all_vocals.pth','HP3_all_vocals.pth','HP5_only_main_vocal.pth',
          'VR-DeEchoAggressive.pth','VR-DeEchoDeReverb.pth','VR-DeEchoNormal.pth']:
    hf_hub_download(repo, f'uvr5_weights/{m}', local_dir='./assets/uvr5_weights')
```

### 3.2 音色模型

音色模型社区来源：

| 渠道 | 地址 | 说明 |
|------|------|------|
| HuggingFace | huggingface.co | 搜 `RVC v2 voice model` |
| ai-search.io | ai-search.io/voices | RVC 模型索引站 |
| ModelScope | modelscope.cn | 国内中文模型 |

挑选标准：**epoch ≥ 300** + **带 .index 索引文件**

```python
# 示例：Disco Elysium Narrator
hf_hub_download('Its7up/Disco-Elysium-Narrator-RVC-v2',
                'de_narrator.pth', local_dir='./assets/weights')
hf_hub_download('Its7up/Disco-Elysium-Narrator-RVC-v2',
                'added_IVF256_Flat_nprobe_1_de_narrator_v2.index',
                local_dir='./logs')
```

模型目录结构：
```
assets/weights/         ← .pth 音色模型
logs/                   ← .index 特征索引（文件名需包含模型名前缀）
```

---

## 4. 启动服务

### 4.1 WebUI 模式

```powershell
# 配置镜像加速
$env:HF_ENDPOINT = "https://hf-mirror.com"

# 启动 (默认端口 7897)
venv\Scripts\python.exe infer-web.py --port 7897
```

浏览器访问 `http://localhost:7897`，通过 Gradio 界面交互式变声。

### 4.2 命令行推理 (Bridge 模式)

```powershell
cd Project-Mozart-merged
python rvc_post_bridge.py -i preprocessor/clean_speech.wav -m "de_narrator.pth"
```

---

## 5. 接入 Project-Mozart

### 5.1 Bridge 架构

[rvc_post_bridge.py](rvc_post_bridge.py) 负责：

```
preprocessor/clean_speech.wav (48kHz)
    │
    ▼ librosa 3:1 降采样
16kHz/mono/f32                        ← Mozart 契约格式
    │
    ▼ VC.vc_single()
RVC 推理管道:
  · load_audio() → 16000Hz
  · HuBERT 特征提取 (fairseq)
  · RMVPE F0 提取
  · VITS Generator 推理 (GPU)
  · FAISS 特征检索 (index)
    │
    ▼
48kHz 变声输出
```

### 5.2 已验证模型

| 模型 | epoch | kHz | index | 大小 | 风格 |
|------|-------|-----|-------|------|------|
| de_narrator | 300 | 48k | ✅ 31MB | 55MB | Disco Elysium 旁白 |
| John Frusciante | 540 | 32k | ✅ 113MB | 54MB | 摇滚男声 |
| Eric Lapointe | — | 40k | ❌ | 53MB | 加拿大摇滚 |
| My-Voice | 150 | 40k | ❌ | 53MB | 通用 |

---

## 6. 性能基准 (RTX 5070 Laptop 8GB)

| 指标 | 数值 |
|------|------|
| 模型加载 | ~3s（首次） |
| HuBERT 特征 | 0.77s / 10s 音频 |
| RMVPE F0 | 1.06s / 10s 音频 |
| GPU 推理 | **0.49s** / 10s 音频 |
| 总延迟 | ~2.3s / 10s 音频 |
| GPU 显存峰值 | ~3.1 GB |
| GPU 利用率峰值 | ~90% (仅推理阶段) |

**RTF (Real-Time Factor)：0.23x** — 10 秒音频在 2.3 秒内处理完毕，实时性充足。
