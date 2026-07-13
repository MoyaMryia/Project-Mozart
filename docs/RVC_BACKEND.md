# Mozart RVC 后端 · 开发指南

> C++17 变声后端，接收预处理契约流，运行 RVC 变声推理
> UDP 定长包 + 原生 socket HTTP REST 管理

---

## 1. 架构定位

```
┌─────────────────────┐    UDP MZRT Contract      ┌────────────────────┐
│ 预处理 (preprocessor/) │ ──── 16kHz/f32/20ms ────► │  RVC Backend      │
│  mozart_pre_process() │    + 16B FrameMeta      │  (C++17, rvc-backend)│
│                       │ ◄── 48kHz/f32/20ms ────── │  · RVC 推理        │
└───────────────────────┘    + FrameMeta 透传      │  · 模型热切换      │
                                                    │  · HTTP 管理       │
                                                    └────────────────────┘
```

- **输入**：16 kHz / mono / float32 / 20 ms（320 样本）+ 16B FrameMeta
- **输出**：48 kHz / mono / float32 / 20 ms（960 样本）+ FrameMeta 透传
- **协议**：UDP 端口 18000，魔数 `0x4D5A5254`（`'MZRT'`）
- **管理 API**：HTTP 端口 18080（原生 socket 实现，无外部 HTTP 库）

---

## 2. 项目结构

```
rvc-backend/
├── CMakeLists.txt           # CMake 构建
├── config.yaml              # 运行时配置（含所有可调参数）
├── include/
│   ├── common.hpp           # 小端读写、socket 工具函数
│   ├── network/
│   │   ├── packet.hpp       # ContractAudioPacket (MZRT 契约包)
│   │   └── udp_server.hpp   # 三线程 UDP 服务器
│   ├── rvc/
│   │   ├── pipeline.hpp     # MockRVCPipeline / RealRVCPipeline + 工厂
│   │   ├── inferencer.hpp   # RVCInferencer（完整推理链定义）
│   │   ├── feature_extractor.hpp  # HuBERT / RMVPE 特征提取
│   │   └── model_loader.hpp       # RVCModel / ModelManager
│   ├── api/
│   │   └── http_api.hpp     # 原生 socket HTTP REST API
│   └── utils/
│       └── config.hpp       # YAML 配置加载（dot-path 缓存）
├── src/
│   ├── main.cpp             # 入口：加载配置 → 创建管线 → 启动 UDP + HTTP
│   ├── network/
│   │   ├── packet.cpp       # pack/unpack 序列化
│   │   └── udp_server.cpp   # 三线程实现
│   ├── rvc/
│   │   ├── pipeline.cpp     # Mock（线性上采样）+ Real（stub 推理）
│   │   ├── inferencer.cpp   # 推理链：resample→F0→HuBERT→index→generator
│   │   ├── feature_extractor.cpp  # HuBERT/RMVPE（Phase 1 全为 stub）
│   │   └── model_loader.cpp       # 模型扫描 + config.json 解析
│   ├── api/
│   │   └── http_api.cpp     # 原生 socket HTTP：/health, /status, /models, /activate
│   └── utils/
│       └── config.cpp       # YAML dot-path 解析 + 缓存
└── tests/
    ├── test_packet.cpp      # ContractAudioPacket 回环测试
    └── test_udp_loopback.cpp # UDP 服务器回环集成测试（端口 19000）
```

---

## 3. 包格式

### 3.1 MZRT 契约包（当前标准）

```c
struct FrameMeta {                  // wire offset
    uint64_t pts_ns;               // +4
    uint32_t frame_idx;            // +12
    uint8_t  vad_flag;             // +16: 1=语音, 0=静音
    uint8_t  energy_db;            // +17: 能量 dB 映射到 0-255
    uint8_t  conf;                 // +18: 去噪置信度 0-255
    uint8_t  segment_id;           // +19: 语音段编号 (0=静音)
};  // wire size = 16 bytes (magic 4 + 16 meta) = 20B header
```

完整布局（小端序）：

```
偏移 0:    magic      (u32)   0x4D5A5254 ('MZRT')
偏移 4:    pts_ns     (u64)
偏移 12:   frame_idx  (u32)
偏移 16:   vad_flag   (u8)
偏移 17:   energy_db  (u8)
偏移 18:   conf       (u8)
偏移 19:   segment_id (u8)
偏移 20:   samples[]  (float32) 单声道 PCM
```

| 方向 | 采样率 | 帧长 | 样本数 | Payload | 总大小 |
|------|--------|------|--------|---------|--------|
| 输入 | 16 kHz | 20 ms | 320 | 1280 B | **1300 B** |
| 输出 | 48 kHz | 20 ms | 960 | 3840 B | **3860 B** |

静音判定：`vad_flag == 0` 时服务器跳过推理，直接返回零帧。

### 3.2 静音判定

`vad_flag == 0` 时服务器跳过推理，直接返回零帧。

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ receive_loop │    │ process_loop │    │  send_loop   │
│ recvfrom()   │──► │ collect_frames│──► │ sendto()     │
│ unpack 校验   │    │ → callback   │    │ pack 回发     │
│ input_buffer │    │ latency 统计  │    │ client_addr  │
└──────────────┘    └──────────────┘    └──────────────┘
```

核心行为：
- **接收**：`recvfrom()` → 校验魔数 → `ContractAudioPacket::unpack()` → 推入 `input_buffer_`
- **处理**：从 buffer 中取出最多 `frames_per_inference` 帧 → 调用推理回调 → 记录延迟
- **发送**：输出帧 `pack()` → `sendto()` 回客户端源地址
- **客户端追踪**：仅第一条合法包的发送方被记录为 `client_addr_`，后续输出全部回发到此地址

关键参数 `InferenceCallback`：
```cpp
using InferenceCallback = std::function<std::vector<float>(const std::vector<float>&)>;
```

---

## 5. RVC 管线

### 5.1 MockRVCPipeline（Phase 1 默认）

线性上采样直通：
- 16kHz → 48kHz（3x 比率）：每个输入样本重复 3 次
- 其他比率：线性插值

用于验证 UDP 回环和链路连通性。

### 5.2 RealRVCPipeline（Phase 2 目标）

构造函数接收 `ModelManager`、`hubert_path`、`rmvpe_path`。

`process()` 完整流程：
1. 检查 inferencer 是否可用（无模型时 fallback 到 mock）
2. `RVCInferencer::infer()` 执行：
   - (a) 重采样到 16kHz（若需要）
   - (b) 提取 F0（`feature_extractor_->extract_f0()` → **Phase 1 返回全零**）
   - (c) 提取 HuBERT 特征（`extract_features()` → **Phase 1 返回全零 [1, T, 768]**）
   - (d) `.index` 检索（`apply_index()` → **Phase 1 stub，返回原特征**）
   - (e) Generator 推理（`run_generator()` → **Phase 1 stub，仅做上采样**）

工厂模式：`RVCPipelineFactory::create(mock_mode, ...)`。
- `mock_mode=true` → `MockRVCPipeline`
- `mock_mode=false` → 扫描 `models_dir`，自动加载第一个可用模型 → `RealRVCPipeline`

### 当前所有推理组件均为 stub

| 组件 | 状态 | 行为 |
|------|------|------|
| HuBERT 特征提取 | **Stub** | 返回全零 [1, T, 768] |
| F0 提取 | **Stub** | 返回全零 |
| FAISS 索引 | **Stub** | 返回特征不变 |
| Generator | **Stub** | 忽略特征和 F0，直接上采样输入到 48kHz |
| 模型加载 | **Stub** | 仅检查文件存在，不做 `torch::load` |

---

## 6. HTTP REST API（原生 socket 实现）

不使用任何 HTTP 库。基于 `socket/bind/listen/accept` + 手动 HTTP/1.1 请求解析。

| 端点 | 方法 | 说明 | 实现状态 |
|------|------|------|----------|
| `/health` | GET | 健康检查 → `{"status":"ok"}` | ✅ |
| `/status` | GET | 服务状态 + 延迟统计 + 契约配置 | ⚠️ `mode` 硬编码为 `"real"`，模型信息硬编码为 `null/false` |
| `/models` | GET | 列出 `models_dir` 下所有模型目录 | ✅ |
| `/models/{id}/activate` | POST | 激活模型 | ⚠️ **Stub**：返回成功但不实际调用 `pipeline_->switch_model()` |

未实现/死代码：
- `POST /models/upload` → `handle_upload_model()` 存在但未被路由，返回 501

---

## 7. 模型管理

### 数据结构

```
models/
└── {model_id}/
    ├── {model_id}.pth       # PyTorch checkpoint
    ├── config.json           # {"data":{"sampling_rate":48000},"model":{"emb_channels":768},"spk":{"id":0},"f0":true}
    └── {model_id}.index      # FAISS 索引（可选）
```

### RVCModel
- `exists()`: 检查 `.pth` + `config.json` 是否存在
- `load()`: 解析 config.json → 调用 `load_generator()`（stub）→ `load_index()`（stub）
- `load_generator()`: **Phase 1 stub**，仅打印日志
- `load_index()`: **Phase 1 stub**，仅打印日志

### ModelManager
- `list_models()`: 扫描 `models_dir` 子目录，检查 `.pth` + `config.json` 存在性
- `load_model(id)`: 创建/获取 RVCModel → 调用 `load()` → 设为当前模型

---

## 8. 配置系统

### Config 类

```cpp
Config::from_yaml("config.yaml");      // 加载 YAML 文件
Config::default_config();              // 自动搜索 config.yaml / ../config.yaml / ../../config.yaml
cfg.get_int("rvc.mock_mode", 1);       // dot-path 类型安全读取（带缓存）
```

### config.yaml 完整结构

```yaml
input:
  contract:
    sample_rate: 16000
    channels: 1
    format: float32
    frame_duration_ms: 20
    bytes_per_sample: 4
  meta:
    enabled: true
    pts_enabled: true
    vad_enabled: true          # 静音 bypass 开关
    energy_enabled: true
    segment_enabled: true
  upsample_to_rvc_sr: true

output:
  sample_rate: 48000
  channels: 1
  format: float32
  convert_to_int16: false

network:
  audio:
    host: "0.0.0.0"
    port: 18000
    frame_duration_ms: 20
    frames_per_inference: 1    # 1=最低延迟, >1=批量推理
  control:
    host: "0.0.0.0"
    port: 18080

rvc:
  mock_mode: true               # true=直通, false=真实推理
  models_dir: "./models"
  assets_dir: "./assets"
  hubert_path: "./assets/hubert/hubert_base.pt"
  rmvpe_path: "./assets/rmvpe/rmvpe.pt"
  sample_rate: 48000
  f0_method: "rmvpe"
  pitch_shift: 0
  index_rate: 0.75
  filter_radius: 3
  rms_mix_rate: 0.25
  protect: 0.33
  device: "cuda"
  half: false
  keep_feature_extractor_warm: true

logging:
  level: INFO
  print_latency_stats: true
  latency_stats_interval_sec: 5
```

> 注意：`rvc.f0_method`、`rvc.pitch_shift`、`rvc.index_rate` 等参数定义在配置中，但当前 main.cpp 未读取（inferencer 使用构造函数默认值）。`input.contract.channels`、`format` 等也已在配置中但未被代码消费——属于预留/文档字段。

---

## 9. 构建与运行

### 依赖

| 库 | 安装方式 |
|----|----------|
| yaml-cpp | `apt install libyaml-cpp-dev` |
| nlohmann/json | FetchContent（CMake 自动拉取） |
| spdlog | FetchContent（CMake 自动拉取） |
| libtorch（可选） | 手动下载，`-DUSE_LIBTORCH=ON` |

### 构建

```bash
cd rvc-backend
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
# UDP 18000 | HTTP 18080 | mock_mode=true → 直通
```

### 测试

```bash
./test_packet           # 包序列化回环 + 非法魔数 + 截断 + 静音检测
./test_udp_loopback     # UDP 回环集成测试（端口 19000，发送 3 帧验证）
```

---

## 10. 前端对接

### 发送

```
每 20ms：
  1. 从 preprocessor/ 取 320 个 float32 + FrameMeta
  2. 打包 1300B：magic + pts_ns + frame_idx + vad_flag
                   + energy_db + conf + segment_id + samples[320]
  3. sendto(后端IP:18000)
```

### 接收

```
recvfrom() ← 后端自动回发到源地址
校验 magic == 0x4D5A5254
解析 960 个 float32 @ 48kHz → 播放器
维护 2~3 帧 jitter buffer
```

### 检查清单

- [ ] 发送 1300B UDP 到 `后端IP:18000`，魔数正确
- [ ] 帧间隔严格 20ms ± 2ms
- [ ] 静音帧 `vad_flag=0` → 后端 bypass 省 GPU
- [ ] 接收 3860B 回包，解析 960 float32 @ 48kHz
- [ ] `GET /health` 确认后端在线
- [ ] `GET /status` 确认配置一致
- [ ] `POST /models/{id}/activate` 切换模型

预处理调用：
```c
mozart_pre_ctx *ctx = mozart_pre_init(&cfg);
mozart_pre_process(ctx, in_48k_960, 960, out_16k_320, &meta);
```
详见 [`PREPROCESSING.md`](PREPROCESSING.md)。

---

## 11. Phase 1 实现状态

| 组件 | 状态 | 说明 |
|------|------|------|
| 项目骨架 + CMake | ✅ | C++17, 三个 target 可编译 |
| ContractAudioPacket | ✅ | pack/unpack + 完整测试 |
| UDP 三线程服务器 | ✅ | 收/处理/发 + VAD bypass + 批量 |
| Mock 直通管线 | ✅ | 线性上采样 16→48kHz |
| Real 管线框架 | ✅ | 异常安全，fallback 到 mock |
| HTTP /health | ✅ | |
| HTTP /status | ⚠️ | 模型/模式信息硬编码 |
| HTTP /models | ✅ | 目录扫描 |
| HTTP /models/activate | ⚠️ | 不实际切换模型 |
| YAML 配置加载 | ✅ | dot-path + 缓存 |
| config.json 解析 | ✅ | sampling_rate, emb_channels 等 |
| HuBERT 特征提取 | ❌ stub | 返回全零 |
| F0 提取 | ❌ stub | 返回全零 |
| FAISS 索引检索 | ❌ stub | 返回特征不变 |
| RVC Generator | ❌ stub | 仅上采样 |
| 模型加载 (.pth) | ❌ stub | 仅检查文件存在 |
| libtorch 集成 | ❌ | 需 `-DUSE_LIBTORCH=ON` |

---

## 12. 已知问题

- `handle_upload_model()` 代码存在但未注册到路由
- `/status` 的 `mode` 和模型信息不查询管线，直接硬编码
- `/models/{id}/activate` 返回成功但不实际调用 `switch_model()`
- 配置中部分字段（如 `f0_method`）定义了但未被 main.cpp 读取
