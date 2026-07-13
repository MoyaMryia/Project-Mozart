# RVC 模型 ONNX 导出指南

> **目的：将 PyTorch `.pth` 模型转为 ONNX `.onnx`，彻底摆脱 PC 端 Python/PyTorch 依赖。**
>
> 只跑一次，之后 Jetson 独立自持。

---

## ⚠️ 给 Contributor 的提醒

**这个仓库是两栖架构：**

```
PC 端 (一次性)              Jetson 端 (日常运行)
┌──────────────┐           ┌────────────────────────┐
│ Python RVC   │  export   │ C++ rvc-backend        │
│ PyTorch .pth │ ────────► │ ONNX Runtime / TensorRT │
│ fairseq      │           │ preprocessor (C11)      │
│ gradio       │           │                        │
│              │           │ 零 Python 依赖          │
│ 用完即弃     │           │ 零 PyTorch 依赖         │
└──────────────┘           └────────────────────────┘
```

- **不要把 PyTorch 模型 (.pth) 直接丢到 Jetson 上**——Jetson 的 PyTorch 是 NVIDIA 定制版，跟 RVC 依赖链不兼容
- **Jetson 只吃 ONNX**——JetPack 自带 ONNX Runtime + TensorRT，原生加速
- **每个新增音色模型必须先 ONNX 导出再部署**

---

## 1. 为什么必须 ONNX 导出

| | PyTorch (.pth) | ONNX (.onnx) |
|---|---|---|
| 读取方式 | `torch.load()` (必须 Python + PyTorch) | ONNX Runtime (C/C++/Python 均可) |
| Jetson 兼容 | ❌ fairseq/torchcrepe/gradio 装不上 | ✅ JetPack 原生包 |
| 依赖 | Python + PyTorch + fairseq + ... | 零：ONNX Runtime 单库 |
| 推理速度 | 基准 | TensorRT 加速后 +40% |
| 模型大小 | ~55MB | ~55MB (不变) |

---

## 2. 三步导出流程

### 2.1 导出 HuBERT 特征提取器

```python
# tools/export_hubert_onnx.py
import torch
import fairseq
from fairseq import checkpoint_utils

# 加载 HuBERT
models, _, _ = checkpoint_utils.load_model_ensemble_and_task(
    ["assets/hubert/hubert_base.pt"], suffix=""
)
model = models[0].eval().cuda()

# 导出
dummy = torch.randn(1, 16000 * 10).cuda()  # 10s audio @ 16kHz
torch.onnx.export(
    model, dummy,
    "hubert_base.onnx",
    input_names=["audio"],
    output_names=["features"],
    dynamic_axes={"audio": {1: "audio_len"}},
    opset_version=17,
)
print("✅ hubert_base.onnx")
```

### 2.2 导出 RMVPE 音高提取器

```python
# tools/export_rmvpe_onnx.py
from infer.lib.rmvpe import RMVPE

rmvpe = RMVPE("assets/rmvpe/rmvpe.pt", is_half=False, device="cuda")
# RMVPE 内部是 PyTorch，转 ONNX 需提取其 torch 模型
model = rmvpe.model.eval().cuda()

dummy = torch.randn(1, 1024, 128).cuda()
torch.onnx.export(
    model, dummy,
    "rmvpe.onnx",
    input_names=["mel"],
    output_names=["f0"],
    opset_version=17,
)
print("✅ rmvpe.onnx")
```

### 2.3 导出音色生成器 (RVC Generator)

```python
# tools/export_generator_onnx.py
import torch, sys
sys.path.insert(0, ".")
from infer.modules.vc.modules import VC
from configs.config import Config

MODEL_NAME = sys.argv[1] if len(sys.argv) > 1 else "de_narrator.pth"

# 加载模型 → 提取 net_g
vc = VC(Config())
vc.get_vc(MODEL_NAME)
net_g = vc.net_g.eval().cuda()

# 构造 dummy 输入 (匹配 RVC v2 generator 接口)
T = 200  # 帧数
feats = torch.randn(1, T, 768).cuda()     # HuBERT features
p_len = torch.tensor([T]).cuda().long()
pitch = torch.randint(0, 400, (1, T)).cuda().long()
pitchf = torch.randn(1, T).cuda()
sid = torch.tensor([0]).cuda().long()

output_name = MODEL_NAME.replace(".pth", ".onnx")
torch.onnx.export(
    net_g,
    (feats, p_len, pitch, pitchf, sid),
    output_name,
    input_names=["feats", "p_len", "pitch", "pitchf", "sid"],
    output_names=["audio"],
    dynamic_axes={
        "feats": {1: "T"},
        "pitch": {1: "T"},
        "pitchf": {1: "T"},
        "audio": {2: "audio_len"},
    },
    opset_version=17,
)
print(f"✅ {output_name}")
```

### 一键导出

```bash
cd Retrieval-based-Voice-Conversion-WebUI

# 基础模型 (所有音色共用)
python tools/export_hubert_onnx.py
python tools/export_rmvpe_onnx.py

# 每个音色模型各跑一次
python tools/export_generator_onnx.py de_narrator.pth
python tools/export_generator_onnx.py "John-Frusciante-319-32-12_540e.pth"

# 产物
ls *.onnx
# hubert_base.onnx   (~400MB)
# rmvpe.onnx          (~10MB)
# de_narrator.onnx    (~55MB)
# John-Frusciante-319-32-12_540e.onnx  (~55MB)
```

---

## 3. 部署到 Jetson

```bash
# PC 上
scp -P 6001 *.onnx moyamryia@36.151.149.214:~/models/
scp -P 6001 *.index moyamryia@36.151.149.214:~/models/

# Jetson 上
cd ~/Mozart/rvc-backend
mkdir -p build && cd build
cmake .. \
    -DCMAKE_BUILD_TYPE=Release \
    -DONNX_RUNTIME_PATH=/usr/lib/aarch64-linux-gnu
make -j6

# 改配置
sed -i 's/mock_mode: true/mock_mode: false/' ~/Mozart/rvc-backend/config.yaml

# 启动
./rvc_backend ~/Mozart/rvc-backend/config.yaml
```

---

## 4. 之后换模型

```bash
# PC 导出新音色
python tools/export_generator_onnx.py 新模型.pth
scp -P 6001 新模型.onnx moyamryia@36.151.149.214:~/models/

# Jetson 热切换，不用重启
curl -X POST http://localhost:18080/models/新模型/activate
```

---

## 5. 依赖关系图

```
音色模型生命周期：

PC (一次性导出)              Jetson (永久运行)
─────────────────            ─────────────────

hubert_base.pth ──export──► hubert_base.onnx ──┐
                                                │
rmvpe.pt ──export──────► rmvpe.onnx ───────────┤
                                                ├── rvc_backend (C++)
音色A.pth ──export──────► 音色A.onnx ───────────┤    ONNX Runtime
                                                │    TensorRT (可选)
音色B.pth ──export──────► 音色B.onnx ───────────┤
                                                │
音色A.index ──直接拷贝──► 音色A.index ───────────┘
音色B.index ──直接拷贝──► 音色B.index

.index 无需转换，FAISS 是纯 C，Jetson 直接编译使用
```

---

## 6. ONNX 导出清单

每新增一个音色模型，检查：

| # | 检查项 | 命令 |
|---|--------|------|
| 1 | ONNX 文件存在 | `ls *.onnx` |
| 2 | 验证 ONNX 结构 | `python -c "import onnx; onnx.checker.check_model('模型.onnx')"` |
| 3 | 输出 shape 正确 | `python -c "import onnxruntime; s=onnxruntime.InferenceSession('模型.onnx'); print(s.get_outputs()[0].shape)"` |
| 4 | 已 scp 到 Jetson | `ssh jetson ls ~/models/` |
| 5 | Jetson 配置文件指向正确路径 | `grep model_path config.yaml` |
| 6 | mock_mode 已关闭 | `grep mock_mode config.yaml` → `false` |
| 7 | 热切换测试通过 | `curl localhost:18080/models/` 能看到新模型 |

---

## 参考

- [JETSON_DEPLOY.md](JETSON_DEPLOY.md) — Jetson 完整部署指南
- [LOCAL_RVC_SETUP.md](LOCAL_RVC_SETUP.md) — PC 端 Python RVC 搭建
- [RVC_BACKEND.md](RVC_BACKEND.md) — C++ RVC 后端开发指南
- [ARCHITECTURE.md](ARCHITECTURE.md) — 系统架构总览
