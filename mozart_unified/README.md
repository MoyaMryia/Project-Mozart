# Mozart Unified - 合并版实时 AI 变声器

> Project Mozart · 后处理合并版 v0.2.0

这是 Mozart-Post（本地声卡）和 UDP-Contract（网络契约）两套实现的**合并版本**，取两者之长：

- **Mozart-Post 的 Engine/Contract 框架**：清晰的模块分层、ONNX Runtime CUDA EP、header-only 依赖
- **UDP-Contract 的批量推理和完整参数**：`frames_per_inference` 批量处理、完整 RVC 参数（pitch/index/filter/rms/protect）
- **统一传输抽象**：本地 ring buffer（dlopen）+ 可选 UDP，运行时配置切换
- **统一输出抽象**：PipeWire virtual source + UDP 回传 + dummy（测试）

## 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                        mozart_unified                           │
├─────────────────────────────────────────────────────────────────┤
│  Source Layer (输入)                                            │
│  ├─ MockSource     (440Hz 正弦波，测试用)                       │
│  ├─ RingSource     (dlopen 本地共享内存，预处理进程产出)         │
│  └─ UdpSource      (UDP 网络接收，跨设备部署)                   │
├─────────────────────────────────────────────────────────────────┤
│  RVC Pipeline (推理)                                            │
│  ├─ MockRVCPipeline  (线性插值上采样，无模型)                   │
│  └─ RealRVCPipeline  (HuBERT → RMVPE → Generator，ONNX)       │
│     ├─ HuBERTExtractor  (语音特征提取，768维)                   │
│     ├─ RMVPEF0          (基频检测，支持rmvpe/harvest/pm)        │
│     └─ Generator        (声码器，16kHz→48kHz)                   │
├─────────────────────────────────────────────────────────────────┤
│  Output Layer (输出)                                            │
│  ├─ DummySink      (丢弃输出，测试用)                           │
│  ├─ PipeWireSink   (虚拟麦克风，其他app可选)                    │
│  └─ UdpSink        (UDP 回传给发包方)                           │
├─────────────────────────────────────────────────────────────────┤
│  HTTP API (管理)                                                │
│  └─ /health, /status, /models, /models/{id}/activate           │
└─────────────────────────────────────────────────────────────────┘
```

## 契约规格

**输入**（从预处理来）：
- 采样率：16 kHz
- 声道：mono
- 位深：float32, [-1.0, 1.0]
- 帧时长：20 ms → 320 samples
- 元数据：16 bytes packed (pts_ns, frame_idx, vad_flag, energy_db, conf, segment_id)

**输出**（到虚拟麦克风或网络）：
- 采样率：48 kHz
- 声道：mono
- 位深：float32
- 帧时长：20 ms → 960 samples

## 快速开始

### 编译

```bash
cd mozart_unified
cmake -B build -G Ninja \
  -DMOZART_BUILD_TESTS=ON \
  -DMOZART_ENABLE_ONNXRUNTIME=OFF \
  -DMOZART_ENABLE_PIPEWIRE=OFF
cmake --build build
```

### 运行 Mock 闭环

```bash
./build/mozart_unified config.toml
# source=mock, pipeline=mock, sink=dummy
```

### 运行测试

```bash
ctest --test-dir build --output-on-failure
```

## 配置说明

`config.toml` 示例：

```toml
[source]
type = "mock"  # 可选: "mock", "ring", "udp"

[rvc]
mock_mode = true  # false = 加载真实模型

[rvc.params]
pitch_shift = 0
index_rate = 0.0
filter_radius = 3
frames_per_inference = 1  # 批量推理帧数

[output]
type = "dummy"  # 可选: "dummy", "pipewire", "udp"
```

## 依赖

- C++17 编译器
- CMake 3.16+
- spdlog (FetchContent 自动拉取)
- nlohmann/json v3.11.3 (FetchContent 自动拉取)
- ONNX Runtime (可选，`-DMOZART_ENABLE_ONNXRUNTIME=ON`)
- PipeWire 0.3 (可选，`-DMOZART_ENABLE_PIPEWIRE=ON`)
- cpp-httplib (vendored, header-only)
- toml++ (vendored, header-only)

## 与原版对比

| 特性 | Mozart-Post | UDP-Contract | **Unified** |
|---|---|---|---|
| 本地 ring | ✅ | ❌ | ✅ |
| UDP 传输 | ❌ | ✅ | ✅ |
| PipeWire 输出 | ✅ | ❌ | ✅ |
| UDP 回传 | ❌ | ✅ | ✅ |
| ONNX Runtime | ✅ | ❌ (libtorch) | ✅ |
| 批量推理 | ❌ | ✅ | ✅ |
| 完整 RVC 参数 | ❌ | ✅ | ✅ |
| 旧协议兼容 | ❌ | ✅ | ✅ (RAVC+MZRT) |
| 依赖数量 | 少 | 多 | 中 |

## 当前阶段

**Phase 1（骨架 + Mock 闭环验证）**：
- ✅ 模块分层架构（source/pipeline/output）
- ✅ 契约包打包/解包
- ✅ Mock 管线端到端（16kHz→48kHz 上采样）
- ✅ UDP 回环测试
- ⏳ 真实模型接入（HuBERT/RMVPE/Generator ONNX 导出 + 推理）
- ⏳ PipeWire 虚拟麦克风实际输出
- ⏳ HTTP API 完整实现

## 下一步

1. 导出 RVC 模型为 ONNX 格式（参考 `ai_voice_changer/docs/onnx_export_guide.md`）
2. 实现 HuBERT/RMVPE/Generator 的真实推理逻辑
3. 在 Jetson Orin Nano 上联调延迟和吞吐
4. 实现 PipeWire sink 的实际 buffer 写入
5. 集成 cpp-httplib 完成 HTTP API

## License

与 Project-Mozart 主项目保持一致。
