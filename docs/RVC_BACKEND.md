# Mozart RVC 后端 · 开发指南

> C++17 RVC 变声后端，接收预处理契约流，运行变声推理
> UDP 定长包 + HTTP REST 管理

---

## 1. 架构定位

```
┌─────────────────┐    UDP MZRT Contract      ┌─────────────────────┐
│ Project Mozart  │ ──── 16kHz/f32/20ms ────► │  RVC Backend        │
│ 预处理 (C, preprocessor/) │    + 12B FrameMeta        │  (C++17, 本仓库)    │
│                 │ ◄── 48kHz/f32/20ms ────── │  · RVC 推理         │
└─────────────────┘    + 12B FrameMeta        │  · 模型热切换       │
                                               │  · HTTP 管理        │
                                               └─────────────────────┘
```

- **输入**：16 kHz / mono / float32 / 20 ms（320 样本）+ 12B FrameMeta
- **输出**：48 kHz / mono / float32 / 20 ms（960 样本）+ FrameMeta 透传
- **协议**：UDP 端口 18000，魔数 `0x4D5A5254`（`'MZRT'`）
- **管理 API**：HTTP 端口 18080

---

## 2. 项目结构

```
rvc-backend/
├── CMakeLists.txt           # CMake 构建（yaml-cpp, nlohmann/json, spdlog）
├── config.yaml              # 运行时配置
├── include/
│   ├── common.hpp           # 通用类型、大小端工具、socket 封装
│   ├── network/
│   │   ├── packet.hpp       # MZRT 契约包 + RAVC 旧包序列化
│   │   └── udp_server.hpp   # 三线程 UDP 服务器
│   ├── rvc/
│   │   ├── pipeline.hpp     # Mock/Real 管线 + 工厂
│   │   ├── inferencer.hpp   # RVC Generator 推理封装
│   │   ├── feature_extractor.hpp  # HuBERT / RMVPE 特征提取
│   │   └── model_loader.hpp       # 模型加载与管理
│   ├── api/
│   │   └── http_api.hpp     # 原生 socket HTTP REST API
│   └── utils/
│       └── config.hpp       # YAML 配置加载
├── src/
│   ├── main.cpp             # 入口：配置→管线→UDP 服务→HTTP API
│   ├── network/
│   │   ├── packet.cpp       # 包封包/解包
│   │   └── udp_server.cpp   # 三线程收/处理/发
│   ├── rvc/
│   │   ├── pipeline.cpp     # Mock（直通）+ Real 管线
│   │   ├── inferencer.cpp   # 推理链：resample→F0→HuBERT→index→generator
│   │   ├── feature_extractor.cpp  # HuBERT/RMVPE stub
│   │   └── model_loader.cpp       # 模型扫描、加载、配置解析
│   ├── api/
│   │   └── http_api.cpp     # HTTP: /health, /status, /models, /activate
│   └── utils/
│       └── config.cpp       # YAML 配置加载（dot-path 解析）
└── tests/
    ├── test_packet.cpp      # 包序列化回环测试
    └── test_udp_loopback.cpp # UDP 回环集成测试
```

---

## 3. 包格式

### 3.1 MZRT 契约包（当前标准）

```c
struct ContractAudioPacket {
    // ── Header: 20 bytes ──
    uint32_t magic;        // 0x4D5A5254 ('MZRT')
    uint64_t pts_ns;       // 纳秒时间戳
    uint32_t frame_idx;    // 帧序号
    uint8_t  vad_flag;     // 1=含语音, 0=静音
    uint8_t  energy_db;    // 能量 dB 映射到 0-255
    uint8_t  conf;         // 预处理器置信度 0-255
    uint8_t  segment_id;   // 语音段编号 (0=静音/未分段)

    // ── Payload: float32 PCM ──
    float    samples[];    // 单声道 float32 样本
};
```

| 方向 | 采样率 | 帧长 | 样本数 | Payload | 总大小 |
|------|--------|------|--------|---------|--------|
| 输入 | 16 kHz | 20 ms | 320 | 1280 B | **1300 B** |
| 输出 | 48 kHz | 20 ms | 960 | 3840 B | **3860 B** |

### 3.2 RAVC 旧包（48kHz/int16 遗留兼容）

```c
struct LegacyAudioPacket {
    uint32_t magic;      // 0x52415643 ('RAVC')
    uint32_t seq;        // 序号
    uint64_t timestamp;  // 微秒时间戳
    uint16_t format;     // 0 = int16 mono 48kHz
    uint16_t samples;    // int16 样本数
    int16_t  payload[];  // int16 PCM
};
```

---

## 4. UDP 服务器（三线程）

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ 接收线程      │    │ 处理线程      │    │ 发送线程      │
│ recvfrom()   │──► │ 帧积累+推理   │──► │ sendto()     │
│ 包校验+入队   │    │ VAD bypass   │    │ 出队发送      │
└──────────────┘    └──────────────┘    └──────────────┘
```

- **接收**：校验魔数 → 存入输入 buffer
- **处理**：积累帧（可配 `frames_per_inference`）→ 调用推理回调 → 输出 buffer
- **发送**：出队 → 回发到客户端源地址
- VAD bypass：`vad_flag==0` 时跳过推理，直接输出静音

---

## 5. RVC 管线

### 5.1 MockRVCPipeline（Phase 1）

16kHz → 48kHz 线性上采样直通（验证 UDP 回环）。

### 5.2 RealRVCPipeline（Phase 2 目标）

1. 输入 16kHz → HuBERT 特征提取（768-dim）
2. F0 提取（RMVPE / harvest / pm）
3. 特征检索（`.index` 查询，placeholder）
4. Generator 推理（libtorch）→ 48kHz 输出

当前 HuBERT/RMVPE 为 stub，返回 dummy 特征。

---

## 6. HTTP REST API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 → `{"status":"ok"}` |
| `/status` | GET | 服务状态 + 延迟统计 + 契约配置 |
| `/models` | GET | 列出已扫描模型 |
| `/models/{id}/activate` | POST | 激活指定模型 |

---

## 7. 构建与运行

### 依赖

| 库 | 用途 | 安装 |
|----|------|------|
| yaml-cpp | 配置解析 | `apt install libyaml-cpp-dev` |
| nlohmann/json | JSON 序列化 | FetchContent（CMake 自动拉取）|
| spdlog | 日志 | FetchContent（CMake 自动拉取）|
| libtorch（可选） | RVC 推理 | 手动下载 Jetson wheel |

### 构建

```bash
cd RVC_structure
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)
```

启用 libtorch：

```bash
cmake .. -DCMAKE_BUILD_TYPE=Release -DUSE_LIBTORCH=ON \
         -DCMAKE_PREFIX_PATH=/opt/libtorch
```

### 运行

```bash
./rvc_backend
# 默认监听 UDP 18000, HTTP 18080
# mock_mode=true 时直通，等待 UDP 输入
```

### 测试

```bash
make -j$(nproc) test_packet test_udp_loopback
./test_packet
./test_udp_loopback
```

---

## 8. 配置（config.yaml）

```yaml
input:
  contract:
    sample_rate: 16000
    format: float32
    frame_duration_ms: 20
  meta:
    enabled: true
    vad_enabled: true         # 静音 bypass

output:
  sample_rate: 48000
  format: float32

network:
  audio:
    host: "0.0.0.0"
    port: 18000
    frames_per_inference: 1    # 1=最低延迟, >1=批量推理
  control:
    host: "0.0.0.0"
    port: 18080

rvc:
  mock_mode: true              # true=直通测试, false=真实推理
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

## 9. 前端对接要点

### 发送流程

```cpp
// 1. 打开 UDP socket → 后端IP:18000
// 2. 每 20ms：
//    从预处理库取 320 个 float32 + FrameMeta
//    打包 1300 bytes: magic(4) + pts_ns(8) + frame_idx(4) + vad(1)
//                    + energy(1) + conf(1) + segment(1) + samples(1280)
//    sendto()
```

### 接收流程

```cpp
// 从同一 socket recvfrom()
// 校验 magic == 0x4D5A5254
// 解析 960 个 float32 → 送入播放器 @ 48kHz
// 维护 2~3 帧 jitter buffer
```

### 检查清单

- [ ] 发送 1300 bytes UDP 到 `后端IP:18000`，魔数正确
- [ ] 帧间隔严格 20ms ± 2ms
- [ ] 静音帧 `vad_flag=0`，后端跳过推理省 GPU
- [ ] 接收 3860 bytes 回包，解析 960 float32 @ 48kHz
- [ ] `GET /health` 确认后端在线
- [ ] `POST /models/{id}/activate` 切换模型

> 预处理调用：使用 `preprocessor/mozart.h` 的 C ABI：
> ```c
> mozart_pre_ctx *ctx = mozart_pre_init(&cfg);
> mozart_pre_process(ctx, in_48k_960, 960, out_16k_320, &meta);
> ```
> 详见 [`PREPROCESSING.md`](PREPROCESSING.md)。

---

## 10. Phase 1 状态

- [x] C++ 项目骨架 + CMake 构建
- [x] MZRT 契约包协议（float32 + 12B 元数据）
- [x] 三线程 UDP 服务器（收/处理/发）
- [x] RVC 推理管线骨架（Mock + Real 双模式）
- [x] 16kHz→48kHz 适配
- [x] VAD 静音 bypass
- [x] HTTP REST API（health/status/models/activate）
- [x] YAML 配置加载
- [x] 单元测试（包协议、UDP 回环）
- [ ] 真实 RVC Generator 接入（libtorch）
- [ ] ONNX/TensorRT 延迟优化
- [ ] 模型上传端点
