# RVC Voice Changer Backend (C++)

> **定位**：接收 **Project Mozart** 预处理后的契约流音频，负责 **RVC 变声推理**。  
> **技术栈**：纯 C++17，原生 socket UDP + 原生 socket HTTP，可对接 libtorch。  
> 预处理（去噪、AEC、AGC、VAD 等）由上游 Project Mozart 负责，本后端**不处理任何预处理**。

---

## 架构定位

```
┌─────────────────┐     UDP Contract Stream      ┌─────────────────────┐
│ Project Mozart  │ ──► (16kHz/f32/20ms + meta)  │  RVC Backend        │
│ 预处理系统 (C++) │                              │  (本仓库, C++17)     │
│                 │ ◄── (48kHz/f32/20ms + meta)  │  · RVC 推理         │
└─────────────────┘                              │  · 模型热切换       │
                                                  │  · HTTP 管理        │
                                                  └─────────────────────┘
```

- **输入**：`16 kHz / mono / float32 / 20 ms` 帧 + 元数据侧带（VAD、energy、segment_id）
- **输出**：`48 kHz / mono / float32 / 20 ms` 帧 + 元数据侧带（透传）
- **协议**：UDP 定长契约包（详见 [`docs/interface_contract.md`](docs/interface_contract.md)）
- **模型推理**：RVC Generator (libtorch，Phase 1 为 stub)

---

## 项目结构

```
RVC_structure/
├── CMakeLists.txt       # 构建配置
├── README.md            # 本文档
├── config.yaml          # 运行时配置
├── docs/
│   ├── protocol_spec.md       # 旧 UDP 协议（遗留文档）
│   └── interface_contract.md  # ⭐ Mozart ↔ RVC 后端接口契约
├── include/             # 公共头文件
│   ├── common.hpp
│   ├── network/
│   │   ├── packet.hpp          # 契约流包协议（序列化/反序列化）
│   │   └── udp_server.hpp     # UDP 服务器（三线程：收/处理/发）
│   ├── rvc/
│   │   ├── pipeline.hpp        # Mock / Real 流水线 + 工厂
│   │   ├── inferencer.hpp      # RVC Generator 推理封装
│   │   ├── feature_extractor.hpp # HuBERT / RMVPE 特征提取
│   │   └── model_loader.hpp    # 模型加载与管理
│   ├── api/
│   │   └── http_api.hpp        # HTTP REST API (原生 socket)
│   └── utils/
│       └── config.hpp          # YAML 配置加载
├── src/                 # 源文件
│   ├── main.cpp
│   ├── network/
│   │   ├── packet.cpp
│   │   └── udp_server.cpp
│   ├── rvc/
│   │   ├── pipeline.cpp
│   │   ├── inferencer.cpp
│   │   ├── feature_extractor.cpp
│   │   └── model_loader.cpp
│   ├── api/
│   │   └── http_api.cpp
│   └── utils/
│       └── config.cpp
└── tests/               # 单元/集成测试
    ├── test_packet.cpp
    └── test_udp_loopback.cpp
```

---

## 依赖

| 库 | 用途 | 安装方式 |
|----|------|----------|
| **yaml-cpp** | 配置文件解析 | `apt install libyaml-cpp-dev` / `vcpkg` / `brew` |
| **nlohmann/json** | JSON 序列化 (HTTP API) | FetchContent (CMake 自动拉取) |
| **spdlog** | 日志 | FetchContent (CMake 自动拉取) |
| **libtorch** (可选) | 真实 RVC 推理 | 手动下载 NVIDIA Jetson wheel |

---

## 构建

### 1. 安装 yaml-cpp

```bash
# Ubuntu/Debian (Jetson 通常用 Ubuntu)
sudo apt-get update
sudo apt-get install -y cmake build-essential libyaml-cpp-dev

# 若 yaml-cpp 版本过低，也可用 FetchContent 替代（修改 CMakeLists.txt）
```

### 2. 构建项目

```bash
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)
```

### 3. 启用 libtorch（真实 RVC 推理）

```bash
# 1. 从 NVIDIA 下载对应 JetPack 版本的 libtorch
#    https://developer.nvidia.com/embedded/downloads
# 2. 解压到 /opt/libtorch 或项目目录
# 3. 构建时指定：
cmake .. -DCMAKE_BUILD_TYPE=Release -DUSE_LIBTORCH=ON \
         -DCMAKE_PREFIX_PATH=/opt/libtorch
```

> 当前 Phase 1 代码中 libtorch 调用为 stub，未启用 `-DUSE_LIBTORCH` 也能编译运行 Mock 模式。

### 4. 测试

```bash
cmake --build . --target test_packet
./test_packet

cmake --build . --target test_udp_loopback
./test_udp_loopback
```

---

## 运行

```bash
# 从项目根目录运行（确保 config.yaml 在路径中）
cd build
./rvc_backend
```

默认监听：
- **UDP 契约流**：`0.0.0.0:18000`（接收 16kHz 输入，返回 48kHz 输出）
- **HTTP API**：`0.0.0.0:18080`

---

## 配置说明

编辑 `config.yaml`：

```yaml
input:
  contract:
    sample_rate: 16000        # 标准契约：16kHz（与 Mozart 预处理对接）
    format: float32
    frame_duration_ms: 20
  meta:
    enabled: true
    vad_enabled: true         # 启用静音 bypass，节省 GPU

output:
  sample_rate: 48000          # RVC 生成器输出 48kHz
  format: float32

network:
  audio:
    host: "0.0.0.0"
    port: 18000
    frame_duration_ms: 20
    frames_per_inference: 1    # 1 = 最低延迟；>1 = 批量推理
  control:
    host: "0.0.0.0"
    port: 18080

rvc:
  mock_mode: true             # true = 直通测试；false = 真实 RVC 推理
  models_dir: "./models"
  hubert_path: "./assets/hubert/hubert_base.pt"
  rmvpe_path: "./assets/rmvpe/rmvpe.pt"
  device: "cuda"
  half: false

logging:
  print_latency_stats: true
  latency_stats_interval_sec: 5
```

---

## HTTP REST API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/status` | GET | 服务状态 + 契约配置 + 延迟统计 |
| `/models` | GET | 列出已扫描模型 |
| `/models/{id}/activate` | POST | 激活指定模型 |

---

## 与 Project Mozart 集成

本后端期望从 Mozart 预处理系统接收 **标准契约流**。

关键对接点：
1. **魔数**：`0x4D5A5254` ('MZRT')
2. **采样率**：输入 `16 kHz`，输出 `48 kHz`
3. **格式**：`float32 PCM`，单声道，20 ms 帧
4. **元数据侧带**：12 字节头（pts_ns、frame_idx、vad_flag、energy_db、conf、segment_id）

详见 [`docs/interface_contract.md`](docs/interface_contract.md)。

---

## Phase 1 状态

- [x] C++ 项目骨架（CMake + 目录结构）
- [x] UDP 契约流协议（float32 + 元数据侧带）
- [x] UDP 服务器（三线程：收/处理/发）
- [x] RVC 推理流水线骨架（Mock / Real 双模式）
- [x] 输入 16kHz → 输出 48kHz 适配
- [x] VAD 静音帧 bypass（节省 GPU）
- [x] HTTP REST API（原生 socket，health/status/models）
- [x] YAML 配置加载
- [x] 单元测试（包协议、UDP 回环）
- [ ] 真实 RVC Generator 接入（libtorch，需模型与依赖验证）
- [ ] ONNX/TensorRT 延迟优化
- [ ] 模型上传端点 (`POST /models/upload`)

---

## 协议文档

- 旧格式：[`docs/protocol_spec.md`](docs/protocol_spec.md)（48kHz/int16，遗留兼容）
- **新契约**：[`docs/interface_contract.md`](docs/interface_contract.md)（Mozart 对接文档）
