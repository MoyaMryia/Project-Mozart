# C++ RVC Backend · 完善实施计划

> Phase 2 — 将 mock 管线替换为真实 RVC 变声推理

## 当前状态（Phase 1）

| 组件 | 文件 | 状态 |
|------|------|------|
| UDP 收发与契约序列化 | `IO/src/udp_stream.cpp` | ✅ 已迁入统一 IO 模块 |
| IO→推理编排 | `src/rvc/audio_worker.cpp` | ✅ VAD bypass + 延迟统计 |
| HTTP REST API | `src/api/http_api.cpp` | ✅ 基本完成，`/activate` 是 stub |
| YAML 配置 | `src/utils/config.cpp` | ✅ 生产就绪 |
| 模型目录扫描 + config.json 解析 | `src/rvc/model_loader.cpp` | ✅ 逻辑完整，加载是 stub |
| Mock 管线（直通上采样） | `src/rvc/pipeline.cpp` | ✅ MockRVCPipeline |
| Real 管线框架 | `src/rvc/pipeline.cpp` | ✅ 骨架完成 |
| HuBERT 特征提取 | `src/rvc/feature_extractor.cpp` | ❌ 返回全零 |
| RMVPE F0 提取 | `src/rvc/feature_extractor.cpp` | ❌ 返回全零 |
| Generator 推理 | `src/rvc/inferencer.cpp` | ❌ 仅上采样 |
| FAISS 索引检索 | `src/rvc/inferencer.cpp` | ❌ 返回原特征 |
| 模型加载 (.pth) | `src/rvc/model_loader.cpp` | ❌ 仅检查文件存在 |
| 模型热切换 | `src/api/http_api.cpp` | ❌ 返回 200 但不调用 switch_model |

---

## 0. 加速引擎选择

### 资源现状

| 资源 | 版本 | 状态 |
|------|------|------|
| **TensorRT** | 10.16.2.10 + `trtexec` | ✅ 已安装 |
| **cuDNN** | 9.20.0.46 | ✅ 已安装 |
| **CUDA Toolkit** | 13.2.1 | ✅ 已安装 |
| **ONNX Runtime C++ SDK** | — | ❌ 未安装 |
| **libtorch (pip, CUDA 12.x)** | `torch/` 下有头文件 + .so | ⚠️ CUDA 12→13 ABI 不兼容 |
| **libtorch (conda, CPU)** | 2.12.1, 头文件可用 | ✅ 可验证 API 逻辑 |
| **FAISS** | — | ❌ 未安装 |

### 推荐方案：ONNX → TensorRT

```
Python (一次性)                C++ rvc-backend (运行时)
┌────────────────────┐         ┌──────────────────────┐
│ RVC WebUI model    │         │ feature_extractor.cpp │
│  ↓ export_onnx.py  │         │      ↓               │
│ .pth → .onnx       │         │ TensorRT Engine API  │
│  ↓ trtexec         │         │      ↓               │
│ .onnx → .trt (fp16)│ ──►     │ inferencer.cpp       │
│                    │         │      ↓               │
│ hubert.trt         │         │ generator + index    │
│ rmvpe.trt          │         │      ↓               │
│ gen_{model_id}.trt │         │ 48kHz float32 output │
└────────────────────┘         └──────────────────────┘
```

**理由：**
- TensorRT 已安装，无需额外依赖
- FP16 延迟比 libtorch + CUDA Graph 更低
- Jetson 上 TensorRT 是 NVIDIA 优化最深的路径
- JETSON_DEPLOY.md 明确推荐此方案用于生产

---

## 1. 模型导出（Python 侧，一次性操作）

### 1.1 环境搭建
- [ ] 安装 Python RVC WebUI 依赖（`LOCAL_RVC_SETUP.md`）
- [ ] 下载基础模型：HuBERT、RMVPE
- [ ] 下载至少 1 个音色模型（.pth + .index）

### 1.2 HuBERT 导出
- [ ] 撰写 `tools/export_hubert_onnx.py`
  - 输入：`[B, 1, T]` waveform @ 16kHz
  - 输出：`[B, T, 768]` content features
- [ ] `trtexec` 编译 `hubert.trt`（FP16）

### 1.3 RMVPE 导出
- [ ] 撰写 `tools/export_rmvpe_onnx.py`
  - 输入：`[B, 1, T]` waveform @ 16kHz
  - 输出：`[B, T]` f0 values
- [ ] `trtexec` 编译 `rmvpe.trt`（FP16）

### 1.4 Generator 导出
- [ ] 撰写 `tools/export_generator_onnx.py`
  - 输入：`phone_ids[B, T]`, `pitch[B, T]`, `features[B, 768, T]`
  - 输出：`[B, 1, T]` waveform @ 48kHz
- [ ] `trtexec` 编译 `gen_{model_id}.trt`（FP16）

### 1.5 验证
- [ ] Python 对比 `.pth` vs `.onnx` 输出一致
- [ ] 三模型 ONNX 输出都正确

---

## 2. 填充 C++ 推理组件

### 2.1 CMake — 添加 TensorRT 支持
```cmake
option(USE_TENSORRT "Enable TensorRT for real RVC inference" OFF)
if(USE_TENSORRT)
    find_package(CUDAToolkit REQUIRED)
    include_directories(${TENSORRT_INCLUDE_DIR})
    target_link_libraries(rvc_backend nvinfer nvinfer_plugin)
endif()
```

### 2.2 `feature_extractor.cpp` — 替换 stub
- [ ] `extract_features()` — 加载 `hubert.trt`，运行 HuBERT 推理
- [ ] `extract_f0()` — 加载 `rmvpe.trt`，运行 RMVPE F0 推理
- [ ] 移除 `hubert_loaded_` 标志 → 用 TensorRT engine 指针替代
- [ ] 添加 engine warming（第一次推理后保持 GPU 状态）

### 2.3 `inferencer.cpp` — 替换 stub
- [ ] `run_generator()` — 加载 `gen_{model_id}.trt`，运行 VITS 推理
- [ ] `apply_index()` — 实现 KNN 最近邻搜索（无需 FAISS，CPU 上暴力搜索即可）
- [ ] `.index` 文件解析（读取 FAISS IVF 索引文件）
- [ ] 接入 `f0`, `pitch_shift`, `index_rate`, `protect` 等参数

### 2.4 `model_loader.cpp` — 替换 stub
- [ ] `load_generator()` — 改为加载 `.trt` 引擎或 `.onnx` 模型
- [ ] `load_index()` — 实现真实 `.index` 文件解析 + 内存加载
- [ ] 添加 TensorRT context 管理（每个模型一个 context）

### 2.5 `http_api.cpp` — 修复 activate
- [ ] `handle_activate_model()` — 调用 `pipeline_->switch_model(model_id)`
- [ ] 热切换：卸载旧引擎 → 加载新引擎 → 重建 inferencer
- [ ] 返回实际切换结果而非硬编码 {"status":"activated"}

---

## 3. 集成测试

### 3.1 单元测试
- [ ] 特征提取：输入已知音频 → 对比 Python 输出
- [ ] Generator：输入固定 features/f0 → 验证输出形状
- [ ] Index 检索：输入特征向量 → 验证最近邻正确
- [ ] 模型加载/卸载/切换：无内存泄漏

### 3.2 集成测试
- [ ] UDP 回环：`test_udp_loopback` 框架 → 发送真实音频 → 验证输出
- [ ] HTTP API：health / status / models / activate 全端点验证

### 3.3 端到端测试
- [ ] 发送 16kHz 真实语音 → 接收 48kHz 变声输出
- [ ] 保存 WAV 试听 → 音色转换效果可接受
- [ ] 静音帧 bypass → 确认 vad_flag 跳过推理

### 3.4 性能测试
- [ ] 单帧延迟 < 20ms（保证实时，配合 20ms 帧间隔）
- [ ] 若后续引入显式批处理接口，验证批处理延迟 < 40ms
- [ ] GPU 显存占用 < 4GB（给系统留余量）
- [ ] 模型热切换 < 2s

---

## 4. 性能优化

| 优化项 | 说明 | 预期提升 |
|--------|------|----------|
| TensorRT FP16 | 已经通过 `trtexec --fp16` 编译 | 延迟降 40% |
| CUDA Graph | 减少 kernel launch 开销 | 延迟降 10% |
| 预处理 + 推理流水线化 | 帧间并行，当前帧推理时收下一帧 | 吞吐 +50% |
| Shared Memory | 预处理 → rvc-backend 零拷贝 DMA | 延迟降 2ms |
| 引擎序列化缓存 | 首次加载 .trt 后序列化，后续 instant start | 启动 3s → 0.5s |

---

## 5. 实施顺序

```
Week 1: 模型导出 + CMake 配通
  ├── Python RVC 环境搭建 + 模型下载
  ├── export_onnx.py 三模型导出
  ├── trtexec 编译 .trt 引擎
  └── CMakeLists.txt 加 TensorRT 依赖

Week 2: feature_extractor + inferencer
  ├── HuBERT + RMVPE TensorRT 推理
  ├── Generator TensorRT 推理
  └── KNN 索引检索

Week 3: model_loader + http_api + 集成
  ├── .trt 引擎加载 + 热切换
  ├── /models/{id}/activate 修复
  ├── 单元测试 + 集成测试
  └── 端到端测试

Week 4: 优化 + 调优
  ├── 性能测试（延迟/显存/吞吐）
  ├── CUDA Graph / 流水线
  └── 文档更新
```

---

## 6. 风险

| 风险 | 缓解 |
|------|------|
| PyTorch ONNX 导出不支持某些 op | 用 TorchScript → ONNX；或用 Python 替代算子 |
| TensorRT 不支持某些 ONNX op | 写 Plugin；或降级 CUDA Graph fallback |
| `.index` 文件格式兼容 | 手写解析器或嵌入轻量 FAISS 源码 |
| Jetson 8GB 显存不足 | FP16 量化；模型分时加载；减少 batch size |
| 实时性不达标 | 保持单帧推理；TensorRT FP16；CUDA Graph |
