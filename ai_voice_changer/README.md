# Mozart Post — Jetson RVC Voice Changer Backend (C++)

实时 AI 变声器后端，运行于 **NVIDIA Jetson Orin Nano 8GB**，消费 Mozart 预处理契约流，输出变声后的 48 kHz 音频到 PipeWire 虚拟麦克风。

- 语言：**C++17** 为主
- 推理：**ONNX Runtime CUDA EP**（HuBERT / RMVPE / Generator）
- 契约输入：16 kHz / mono / float32 / 20 ms / 320 samples + 12 B 元数据
- 输出：48 kHz / mono / float32 / 20 ms / 960 samples → PipeWire virtual source
- 模型管理：cpp-httplib HTTP REST
- 训练 .pth → ONNX 转换：客户端完成（见 `docs/onnx_export_guide.md`）

## 架构

```
真实麦克风 ─► PipeWire ─► [Mozart 预处理 · Rust · 另做]
                             │ 契约流 16kHz/f32/20ms + 12B meta
                             │ ── 契约边界 ──
                             ▼
                     [C++ RVC 后处理 · 本工程]
                             │ ONNXRuntime CUDA EP
                             ▼
                     PipeWire virtual source (48kHz)
                             ▲
                             │ 其它应用可选为"麦克风"
```

## 项目结构

```
ai_voice_changer/
├─ CMakeLists.txt
├─ config.toml
├─ include/                  # 公共头
│  ├─ frame_meta.hpp         # 12B 元数据(与 #[repr(C)] 对齐)
│  ├─ mozart_post.h          # 后处理侧 C ABI(attach/poll/detach)
│  ├─ contract/              # ContractSource ABC + MockSource + MozartRingSource
│  ├─ rvc/                   # RVCPipeline + ModelLoader + HuBERT + RMVPE + Generator
│  ├─ inference/             # InferEngine ABC + OnnxEngine
│  ├─ output/                # OutputSink ABC + DummySink + PipeWireSink
│  └─ server/                # HttpServer (cpp-httplib)
├─ src/                      # 镜像 include
├─ tests/                    # frame_meta_test / mock_pipeline_test / onnx_smoke_test
├─ assets/                   # hubert_base.onnx, rmvpe.onnx (Jetson 预装, gitignored)
├─ models/                   # 上传的说话人 ONNX 模型 (gitignored)
├─ third_party/              # cpp-httplib, toml++ (header-only, 见下方克隆)
├─ docs/
│  ├─ contract_spec.md
│  └─ onnx_export_guide.md
└─ README.md
```

## 依赖 (Jetson)

```bash
sudo apt install -y \
  build-essential cmake ninja-build pkg-config \
  libpipewire-0.3-dev
# OnnxRuntime for Jetson CUDA: 见 NVIDIA Jetson Zoo
# cpp-httplib 与 toml++ 为 header-only,见下方 cloned
```

## 获取 third_party 头文件库

```bash
mkdir -p third_party/cpp-httplib/include third_party/tomlplusplus/include
curl -L https://raw.githubusercontent.com/yhirose/cpp-httplib/master/httplib.h \
  -o third_party/cpp-httplib/include/httplib.h
curl -L https://raw.githubusercontent.com/marzer/tomlplusplus/master/toml.hpp \
  -o third_party/tomlplusplus/include/toml.hpp
```

## 构建 (Jetson)

### Mock 闭环 (无任何上游依赖)

```bash
cmake -B build -G Ninja
cmake --build build
./build/mozart_post config.toml
```

此时：source = mock(正弦 440Hz)→ mock RVC(线性上采样 16k→48k)→ dummy sink。
所有 I/O 链路被打通,日志打印逐帧延迟。

### 启用 ONNXRuntime (真实推理)

```bash
cmake -B build -G Ninja \
  -DMOZART_ENABLE_ONNXRUNTIME=ON \
  -DONNXRUNTIME_ROOT=/path/to/onnxruntime
cmake --build build
```

需先准备 `assets/hubert_base.onnx`、`assets/rmvpe.onnx`，并把 `config.toml` 的
`rvc.mock_mode` 改为 `false`。

### 启用 PipeWire 虚拟源

```bash
cmake -B build -G Ninja -DMOZART_ENABLE_PIPEWIRE=ON
cmake --build build
# config.toml: output.sink = "pipewire"
```

## 配置 (`config.toml`)

```toml
[contract]
source = "mock"          # mock | mozart

[rvc]
mock_mode = true         # true = 直通上采样测试; false = ONNXRuntime 推理

[output]
sink = "dummy"           # dummy | pipewire

[server]
port = 18080
```

## 测试

```bash
ctest --test-dir build --output-on-failure
```

- `frame_meta_test`: 验证 12B 对齐 + 小端布局 + 往返
- `mock_pipeline_test`: MockSource → MockPipeline → DummySink，确认采样数
- `onnx_smoke_test`: 需 assets；缺失则返回 77(SKIP)

## HTTP API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/status` | 模式/延迟统计/当前模型 |
| GET | `/models` | 列出已上传 |
| POST | `/models/upload` | multipart 上传 `.onnx` + `config.json` |
| POST | `/models/{id}/activate` | 切换当前说话人 |

## 与 Mozart 预处理的集成

当上游 `libmozart_pre.so` 就绪后,把 `config.toml` 改为:

```toml
[contract]
source = "mozart"
ring_name = "mozart_pre_out"
```

`MozartRingSource` 会 `dlopen` 它并 `mozart_post_attach(ring_name)`，按
`docs/contract_spec.md` 定义消费契约帧。

## Phase 2 状态

- [x] C++ 工程骨架(CMake + 头/源镜像)
- [x] 契约消费侧(ABC + MockSource + MozartRingSource dlopen)
- [x] RVC 推理骨架(MockPipeline + RealPipeline 接口)
- [x] ONNX 推理引擎(OnnxEngine,CUDA EP，需 `MOZART_ENABLE_ONNXRUNTIME`)
- [x] 输出层(DummySink + PipeWireSink 骨架，需 `MOZART_ENABLE_PIPEWIRE`)
- [x] HTTP 模型管理(cpp-httplib,需 `MOZART_ENABLE_HTTP`)
- [x] 主循环(VAD 跳推理 + segment_id 重置 + 延迟统计)
- [ ] ONX 模型 I/O 名实测校准(拿到真实 ONNX 后微调)
- [ ] `.index` 特征检索(Phase 3,本期固定 `index_rate=0`)

## 协议文档

- 契约规范：`docs/contract_spec.md`
- 客户端 ONNX 导出：`docs/onnx_export_guide.md`