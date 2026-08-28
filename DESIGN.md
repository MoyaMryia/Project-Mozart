# Mozart · 系统设计

> 本项目唯一的设计文档。内容：目标、总体架构、核心契约、各子系统设计、实时性策略、模型交付、实现路线与当前状态、部署、已知缺口。
>
> 维护原则：**代码是唯一事实来源**，本文与代码同步演进，不写"未来也许会"的过度设计。

---

## 1. 目标

- **产品定位**：一块边缘板子当"AI 声卡"（见 [TARGET.md](TARGET.md)）。比赛 demo 为近期交付，代码与文档按可长期演进的产品标准建设。
- **核心指标**：
  - 变声路（实时路）端到端延迟 **< 30ms**，不爆音、不打断。
  - 字幕路（文字路）容忍 1~2 秒延迟，优先准确率。
- **两路并行**（一块板子带得动的前提）：
  - **实时路**：降噪 → RVC 变声（路线第 1 步，实现中）
  - **文字路**：ASR 转写 → 本地 Qwen(0.8B) 翻译 → 字幕（路线第 2 步）
- **平台**：NVIDIA Jetson Orin Nano Super 8GB（JetPack R39，ONNX Runtime / TensorRT，6×Cortex-A78AE + Ampere SM8.7 GPU，7.4GiB LPDDR5 共享内存）。

## 2. 总体架构

### 2.1 数据流

```
[麦克风/UDP] ──► [ IO ] ──► [ 预处理 C11 ] ──契约流──► [ RVC 后处理 C++17 ] ──► [ IO ] ──► [扬声器/UDP]
                  IO/            preprocessor/                rvc-backend/
               设备与协议        48k raw → 16k input           16k input → 48k output
```

文字路（第 2 步）：预处理输出帧旁路喂给 ASR 模块，ASR → 翻译 → 字幕独立消费，不占用变声路的 GPU 实时预算。

### 2.2 组件

| 子系统 | 目录 | 语言 | 职责 | 状态 |
|--------|------|------|------|------|
| IO | `IO/` | C++17 + C ABI | 统一契约帧、UDP/PipeWire/Mock 驱动、SPSC 无锁环 | ✅ UDP 完整；PipeWire stub |
| 预处理 | `preprocessor/` | C11 | HPF、RNNoise 降噪、自适应混合、3:1 降采样 | ✅ 可用 |
| 后处理 | `rvc-backend/` | C++17 | AudioWorker 编排、ONNX 推理、HTTP 管理 | ✅ 主链路（CPU EP） |
| 状态管理 | `state_manager/` | — | 4 模式编排、显存置换、任务队列 | 📐 设计完成，未编码 |
| ONNX 导出 | `tools/` | Python | .pth → .onnx（PC 端一次性） | ✅ |

### 2.3 关键原则

- **IO 不解释算法**：IO 是算法无关的传输通道；特征滑动窗口等上下文逻辑属于推理层内部。
- **预处理/后处理不持有设备与网络资源**：流的生命周期由上层（state_manager）通过 IO 门面独立启停。
- **契约帧唯一定义源**：`IO/include/mozart/frame_meta.h`。`preprocessor/mozart.h` 与 rvc-backend 均 include 该头，禁止各自复制定义。
- **两栖架构**：Jetson 零 Python、零 PyTorch 依赖，只跑 ONNX Runtime（详见 §7）。

## 3. 核心契约

### 3.1 音频格式

| 项 | raw（设备→预处理） | input（预处理→后处理） | output（后处理→设备） |
|---|---|---|---|
| 采样率 | 48 kHz | 16 kHz | 48 kHz |
| 帧长 | 20 ms / 960 样本 | 20 ms / 320 样本 | 20 ms / 960 样本 |
| 格式 | float32 mono [-1,1] | float32 mono [-1,1] | float32 mono [-1,1] |
| 帧大小 | 3856 B | 1296 B | 3856 B |

16kHz input 帧选 16kHz 是因为 RVC 特征提取（HuBERT）原生吃 16kHz；output 48kHz 与声卡原生采样率一致，避免播放端再重采样。

### 3.2 16 字节帧元数据 `mozart_frame_meta_t`

```c
#pragma pack(push, 1)
typedef struct {
    uint64_t pts_ns;       // 呈现时间戳（纳秒），IO 采集时填充
    uint32_t frame_idx;    // 单调递增帧序号
    uint8_t  vad_flag;     // 0=静音 1=语音（预处理填写；静音帧后端跳过推理）
    uint8_t  energy_db;    // 能量 dB，映射到 0-255
    uint8_t  conf;         // 去噪置信度 0-255
    uint8_t  segment_id;   // 语音段编号（0=静音间隔，变化时重置流式状态）
} mozart_frame_meta_t;     // 严格 16 字节，static_assert 保证
#pragma pack(pop)
```

`pts_ns` / `frame_idx` 由 IO 模块在采集回调中填充，预处理与后处理只读不改。

### 3.3 UDP MZRT 契约包（跨网络模式）

```
偏移 0:  magic     u32  0x4D5A5254 ('MZRT')
偏移 4:  pts_ns    u64
偏移 12: frame_idx u32
偏移 16: vad_flag | energy_db | conf | segment_id  (4 × u8)
偏移 20: samples[] float32 单声道 PCM
```

- 输入包 20 + 1280 = **1300 B**（< MTU 1500，杜绝分片丢包）；输出包 20 + 3840 = **3860 B**。
- 端口 **18000**；`vad_flag == 0` → 后端跳过推理直接回零帧（省 GPU）。
- 客户端追踪：首个合法包的发送方地址被记录，输出固定回发该地址。
- 包头（去 magic 后）与 `mozart_frame_meta_t` 布局一致，解包即得元数据。

## 4. IO 子系统（`IO/`）

### 4.1 流抽象（策略模式）

```
AudioStream (Open/Close/IsOpen)
├── RealTimeAudioStream (ReadFrame / WriteFrame / GetUnderlyingLatencyNs)
│   ├── PipeWireStream    本地物理声卡          ⚠️ 当前 stub（见 §4.5）
│   ├── UdpStream         实时网络契约包        ✅ 完整（452 行，严格包校验 + SPSC 输入环）
│   └── MockAudioStream   测试：WAV 伪 20ms 帧  ✅
└── OfflineAudioStream (ReadChunk / WriteChunk)  离线批量吞吐模式  ⬜ 未实现（路线第 3 步）
```

- **帧类型由流方向决定**：Capture 流 ReadFrame 产出 `mozart_raw_frame_t`（PipeWire 48k）或 `mozart_input_frame_t`（UDP 16k）；Playback 流 WriteFrame 消费 `mozart_output_frame_t`。`buf_size` 运行时校验。
- **构造与打开分离**：`create_*_stream → open(sample_rate, frame_ms, ring_capacity) → read/write → close → destroy`，state_manager 可保留配置并独立启停底层资源。
- **C-ABI**（`mozart/audio_io.h`）：`mozart_io_create_pipewire_stream` / `mozart_io_create_udp_stream` / `mozart_io_open_stream` / `mozart_io_read_frame` / `mozart_ring_create|push|pop` 等。所有函数经 `cabi_guard` 收敛异常，保证 C 调用方永不抛出。

### 4.2 SPSC 无锁环（`ring_buffer.hpp`）

- `std::atomic<uint64_t>` write/read 索引；capacity 向上取 2 的幂，位掩码代替取模。
- 读写索引分离到不同缓存行（`alignas(64)`），避免 false sharing。
- 预分配定长存储；运行期 push/pop 只做定长 memcpy + 原子索引更新，**零 malloc、零 mutex**。
- 队满 push 返回 false（物理线程不阻塞，丢帧优于卡死）。
- `readable_count()` 供丢帧追赶判定。

### 4.3 实时性策略

| 机制 | 说明 |
|------|------|
| 双环异步解耦 | 采集/播放按硬件时钟独立运行；推理在独立 Worker 线程，IO 回调绝不被 GPU 耗时（10~30ms 波动）阻塞 |
| 线程优先级 | 物理 IO 线程 `SCHED_FIFO`，推理常规优先级（state_manager 阶段统一落地，见 §6.4） |
| 丢帧追赶 | 输入环积压 > 4 帧（>80ms）说明延迟累积：丢弃最旧 N-1 帧，仅推理最新一帧，20ms 内把累积延迟清零；被丢帧补静音 |
| XRun 保护 | 输出环为空时物理输出线程回填全零静音帧，防止声卡爆音 |

### 4.4 目录结构

```
IO/
├── include/mozart/
│   ├── frame_meta.h        契约帧唯一定义源（16B meta + raw/input/output 帧 + MZRT 常量）
│   ├── audio_io.h          C-ABI（流工厂 + SPSC 环）
│   ├── audio_stream.hpp    C++ 流抽象（AudioStream / RealTime / Offline）
│   ├── ring_buffer.hpp     SpscRing（C-ABI mozart_ring_* 的 C++ 实现）
│   ├── pipewire_stream.hpp / udp_stream.hpp / mock_stream.hpp
├── src/  audio_stream.cpp · ring_buffer.cpp · udp_stream.cpp · pipewire_stream.cpp · mock_stream.cpp
└── tests/  test_ring_buffer.cpp · test_mock_stream.cpp
```

### 4.5 当前状态

- `UdpStream`：✅ 完整（定长包校验、客户端追踪、SPSC 输入环、sendto 回发）。
- `MockAudioStream`：✅ 完整（读 WAV 产生伪 20ms 定时帧，供无设备测试）。
- `PipeWireStream`：⚠️ **stub**——Capture 填静音 PCM、Playback 丢弃 PCM。真实 libpipewire 集成（`MOZART_IO_ENABLE_PIPEWIRE` 编译开关）待第 1 步收尾实现：`pw_stream` capture/playback，20ms quantum。
- `OfflineAudioStream`：⬜ 未实现（路线第 3 步：WebSocket/文件批量）。

## 5. 后处理 rvc-backend（`rvc-backend/`，C++17）

### 5.1 进程结构（`main.cpp`）

```
config.yaml ─► RVCPipelineFactory::create(mock_mode?)
                     │
UdpStream(18000, Capture) ─► AudioWorker::start()      # 独立推理线程
                              │
HttpApiServer(18080)                                   # 管理面
```

- `AudioWorker` 只持有 stream 与 pipeline 的**引用**，不拥有 socket/设备 → state_manager 可独立编排启停（`stop()` 时 Close stream 以解除阻塞的 ReadFrame）。
- 每帧流程：`ReadFrame` → **VAD bypass**（`skip_silence && vad_flag==0` → 零帧 + bypass 计数）→ `pipeline.process()` → `WriteFrame`。
- 统计：每帧推理延迟（avg/max）、inference/bypass 计数，定期打印并经 `/status` 暴露。

### 5.2 Pipeline（工厂 + 双实现）

```
RVCPipelineFactory::create(mock_mode, models_dir, hubert, rmvpe, ...)
  mock_mode = true   → MockRVCPipeline    # 3x 重复 / 线性上采样直通，验证链路连通性
  mock_mode = false  → RealRVCPipeline    # 扫描 models_dir，自动加载首个可用模型
```

`RealRVCPipeline` = `ModelManager` + `FeatureExtractor` + `RVCInferencer`。`process()` 失败时自动 fallback 到 mock 上采样——**链路永不中断**（demo 稳定性优先）。`switch_model(id)` 重载 Generator 并重建 inferencer，HuBERT/RMVPE 常驻不重载。

### 5.3 推理链（`RVCInferencer::infer`）

```
input (16kHz)
  → resample（如需）
  → F0 提取          RMVPE onnx（mel → f0 帧序列）
  → HuBERT 特征      hubert_base.onnx（audio → [T, 768]）
  → index 检索       FAISS IVF：最近质心 + 倒排表 KNN1，按 index_rate 混合
  → Generator onnx   输入 feats / p_len / pitch / pitchf / sid → audio (48kHz)
  → protect 混合     输出与原始音频按 protect 比例混合，保留原声特征
```

组件实现：

- **OnnxEngine**（`onnx_engine.cpp`）：ONNX Runtime C++ API；`Ort::Env` + `Ort::Session`，`IntraOpNumThreads(2)`、全图优化。当前 **CPU EP**；TensorRT EP / FP16 为第 1 步收尾项（Orin Nano 达标 30ms 必需）。
- **FeatureExtractor**（`feature_extractor.cpp`）：HuBERT / RMVPE 各持一个 OnnxEngine，构造时按路径加载。F0 方法 `rmvpe` 可用；`harvest` / `pm` 为占位（返回全零）。
- **IndexSearch**（`index_search.cpp`）：自研 FAISS IVF `.index` 二进制解析（magic `IwFl`，质心 + 倒排表），**无 FAISS 运行时依赖**；`search()` 逐帧最近质心 + KNN1 混合。
- **ModelManager / RVCModel**（`model_loader.cpp`）：模型目录约定 `models/<id>/{<id>.onnx, config.json, <id>.index}`；解析 config.json（sampling_rate / emb_channels / spk_id / has_f0）；`list_models()` 扫描目录；`switch_model()` 即"重载 Generator + 重建 inferencer"。

### 5.4 当前实现缺口（第 1 步收尾清单）

| 项 | 现状 | 说明 |
|----|------|------|
| mel 谱图 | ⚠️ 占位 | `compute_mel` 是能量近似实现，非 FFT + mel 滤波器组；RMVPE 的 128 维 mel 输入需替换为真实实现 |
| F0 方法 | ⚠️ 部分 | 仅 `rmvpe`(onnx) 可用；harvest/pm 返回全零 |
| TensorRT EP / FP16 | ❌ | 当前 CPU EP；Jetson 压测前必须切换 |
| `.pth` 加载 | ❌ | 需 `-DUSE_LIBTORCH=ON` 且未实现；路线统一走 ONNX，**不做** |
| HTTP `/models/upload` | ❌ | 死代码未挂路由；路线第 3 步（网页上传）时实现 |
| Jetson 实机压测 | ⚠️ | `/status` 有 avg/max 统计，实机延迟未验证 |
| `config.yaml` 部分字段 | ⚠️ | `f0_method`/`pitch_shift`/`index_rate` 等已定义但由 inferencer 构造默认值消费，main 未透传 |

### 5.5 HTTP API（端口 18080，原生 socket，零依赖）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | `{"status":"ok"}` |
| `/status` | GET | 模式（mock/real）、当前模型信息、延迟统计、bypass 计数、契约配置 |
| `/models` | GET | 扫描 models 目录（exists / current） |
| `/models/{id}/activate` | POST | 实际调用 `switch_model` 热切换音色，返回 activated/failed |

### 5.6 配置（`config.yaml`）

| 键 | 默认 | 说明 |
|----|------|------|
| `rvc.mock_mode` | false | true=Mock 直通，false=真实 ONNX 推理 |
| `rvc.hubert_path` / `rvc.rmvpe_path` | `./assets/...onnx` | 特征提取模型路径 |
| `rvc.device` / `rvc.half` | cuda / false | 推理设备与 FP16（共享内存有限，谨慎开启） |
| `rvc.pitch_shift` / `index_rate` / `protect` | 0 / 0.75 / 0.33 | 变声参数 |
| `network.audio.port` | 18000 | UDP 音频契约流 |
| `network.control.port` | 18080 | HTTP 管理 |
| `input.meta.vad_enabled` | true | 静音帧 bypass 开关 |
| `logging.print_latency_stats` | true | 周期打印延迟统计 |

### 5.7 构建

```bash
cd rvc-backend && mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DUSE_ONNX=ON && make -j6
./rvc_backend ../config.yaml
# 测试：./test_udp_loopback（IO + AudioWorker + Mock 闭环）
#       ./test_feature_extractor / ./test_inferencer
```

依赖：`libyaml-cpp-dev`（apt）；nlohmann/json、spdlog（CMake FetchContent）；ONNX Runtime（`libonnxruntime-dev`）。

## 6. 状态管理 state_manager（📐 设计完成，未编码）

### 6.1 定位：控制面 / 数据面解耦

state_manager 是全局生命周期与资源编排器，**不触碰任何音频数据**，只通过三个门面间接指挥全链路：

- **Model Facade**：模型加载/卸载（`pipeline->switch_model`）
- **IO Facade**：设备流生命周期（`mozart_io_create/open/close/destroy`）
- **Worker Facade**：工作线程启停（`AudioWorker::start/stop`）

实时模式切换固定顺序：`stop worker → close IO → (跨大类时) unload/load model → open IO → start worker`。

### 6.2 四模式状态空间（2×2 正交）

|  | RVC (ONNX) | Zero-Shot |
|---|---|---|
| **实时**（PipeWire/UDP） | `RT_RVC` ← 路线第 1 步 | `RT_ZERO_SHOT` ← 路线第 4 步 |
| **文件**（批量上传） | `FILE_RVC` ← 路线第 3 步 | `FILE_ZERO_SHOT` ← 路线第 4 步 |

### 6.3 强互斥状态机

任意时刻仅一个模式 ACTIVE（Jetson 8GB 共享内存不允许两路推理并发抢显存）：

| 当前状态 | 目标状态 | 切换逻辑 |
|---|---|---|
| `RT_*` | 任意 | **立即切换**：中断实时流，断开 PipeWire（保护隐私） |
| `FILE_*`（任务运行中） | 任意 | **优雅等待**：写入 pending 槽，文件转完 EOF 后自动执行 |
| `FILE_*`（空闲） | 任意 | **立即切换** |

生命周期动作：
- 切到 File 模式：释放物理麦克风/扬声器设备（隐私 + 避免独占冲突）。
- 同大类切换（`RT_RVC ↔ FILE_RVC`）：**不重载模型**，仅重定向 IO 流，亚秒级完成。
- 跨大类切换（RVC ↔ Zero-Shot）：卸载显存 → 垃圾回收（`cudaDeviceReset`）→ 加载新模型，防 8GB OOM。

显存置换预算：同大类换音色 ~200ms（仅换 Generator，HuBERT/RMVPE 常驻）；RVC ↔ Zero-Shot 3~6s（整实例置换）。

### 6.4 双通道算力调度

- **CPU**：实时 IO 线程 `SCHED_FIFO`（priority 90）；离线文件转换线程 `nice 19` 极低优先级。
- **GPU**：实时推理提交到高优先级 CUDA stream（`cudaStreamCreateWithPriority(0, -1)`），离线批量走低优先级 stream，硬件级抢占。

### 6.5 文件批处理（路线第 3 步）

- **内存 FIFO 任务队列**：串行度 1（8GB 不支持并发推理）；深度上限 50，超限拒绝；RT 模式下队列**暂停消费**（不抢算力），切出后自动恢复。
- **异步 Job 模式**：`POST /api/file/convert` 立即返回 `job_id`，前端轮询 `GET /api/file/status?job_id=`（弱网不断连）。
- **解码适配**：FFmpeg/libav → 16kHz mono f32（MP3/M4A/AAC/FLAC/OGG/WAV）。
- **缓存逐退**：`storage/temp/` 扁平目录，微秒时间戳命名（`[ts]_input.mp3` / `[ts]_output.wav`，天然按时间排序）；每次落盘后审计容量，超 1GB 高水位则按文件名升序删除至 800MB 低水位。
- **API 契约**：`POST /api/mode/switch`、`POST /api/speaker/register`（零样本角色，路线第 4 步）、`POST /api/file/convert`。

## 7. 两栖架构：模型交付流

```
PC (RTX + PyTorch + RVC WebUI)
  │  一次性导出（tools/*.py，opset 17）
  │    hubert_base.pt ──► hubert_base.onnx     (fairseq)
  │    rmvpe.pt       ──► rmvpe.onnx
  │    <音色>.pth     ──► <音色>.onnx  (+ .index)
  ▼  scp
Jetson:
  rvc-backend/assets/hubert/hubert_base.onnx
  rvc-backend/assets/rmvpe/rmvpe.onnx
  rvc-backend/models/<id>/<id>.onnx + config.json + .index
  │
  └─► 日常运行：纯 ONNX Runtime，无 Python / 无 PyTorch
```

- 导出脚本：`tools/export_hubert_onnx.py`、`tools/export_rmvpe_onnx.py`、`tools/export_generator_onnx.py <model.pth> [--all]`。
- `rvc_post_bridge.py`：PC 端验证桥（本地 Python RVC 服务接入 Mozart 契约流），用于在 Jetson 模型就绪前验证全链路，**非产品路径**。
- PC 端 RVC 环境搭建（Windows + CUDA + fairseq/pyworld + 模型下载）见 git 历史 `docs/LOCAL_RVC_SETUP.md`（已并入本文档体系，不再单独维护）。

## 8. 实现路线与当前状态

| # | 目标 | 涉及组件 | 状态 |
|---|------|---------|------|
| 1 | **实时降噪 + RVC 变声** | preprocessor ✅ / IO ✅(UDP) / rvc-backend ✅(CPU EP) | **当前**：收尾 §5.4 缺口（真实 mel、TensorRT EP）+ PipeWire 真驱动 + Jetson 压测 |
| 2 | **ASR 转写 + Qwen 翻译字幕** | 新模块 asr-translator（待设计：输入旁路 16k 契约帧，流式 ASR + 本地 Qwen 0.8B） | ⬜ |
| 3 | **离线文件 / 网页上传变声** | IO OfflineStream + FFmpeg 解码 + 任务队列 + HTTP/WS API | ⬜ |
| 4 | **Zero-Shot 零样本变声** | state_manager 4 模式落地 + 新推理引擎 + 角色注册 | ⬜ |

已完成的基线（第 1 步前半）：

- [x] 预处理全链路（HPF/RNNoise/自适应混合/3:1 降采样，C-ABI `mozart_pre_*`）
- [x] IO 契约帧唯一定义源 + UDP 驱动 + SPSC 无锁环 + C-ABI
- [x] RVC 后端 ONNX 主链路（引擎/特征/推理/模型管理/HTTP API/Index 检索）
- [x] ONNX 导出脚本 ×3；Jetson JetPack R39 环境就绪

## 9. 部署（Jetson Orin Nano Super 8GB）

- **系统**：JetPack R39（L4T），ONNX Runtime 随 JetPack 提供；开启 max-performance 模式（`nvpmodel` + `jetson_clocks`），配置 swap 防 OOM。
- **构建**：
  - preprocessor：`make -j6`（Makefile 指定 `-march=armv8.2-a+dotprod+fp16 -mtune=cortex-a78ae`，RNNoise 自动走 NEON 路径）
  - rvc-backend：`cmake -DUSE_ONNX=ON`（依赖 libonnxruntime-dev、libyaml-cpp-dev）
- **端口**：UDP 18000（音频）、HTTP 18080（管理）。
- **内存预算（7.4GiB LPDDR5 共享）**：RVC 三模型（HuBERT+RMVPE+Generator）常驻 ~2GB；OS/桌面 ~1.5GB；余量预留给第 2 步 ASR+Qwen 与系统峰值。
- **服务化**：systemd unit + 启动脚本，第 1 步收尾后固化（随 PipeWire 真驱动一起交付）。
- **排障**：GPU OOM → 关 `half` 或减少常驻模型；爆音 → 检查输出环 XRun 统计并确认丢帧追赶生效；模型加载失败 → 核对 ONNX opset 与路径。

## 10. 关键设计决策

| 决策 | 理由 |
|------|------|
| C-ABI 契约帧单一来源（frame_meta.h） | 消除 C/C++（及未来其他语言）跨层 ABI 漂移 |
| SPSC 无锁环 + 预分配 | 零运行时 malloc、无锁竞争、确定性延迟 |
| 双环异步 + 丢帧追赶 + 静音回填 | GPU 抖动（10→30ms）不打挂物理音频流；demo 不能爆音 |
| 两栖架构（ONNX Runtime） | Jetson 零 Python/PyTorch 依赖；导出一次性完成 |
| 模型热切换 HTTP API | 运行时零停机换音色（仅换 Generator ~200ms） |
| 控制面/数据面解耦（三 Facade） | state_manager 独立演进，不碰数据面 |
| 强互斥单活跃模式 | 8GB 共享内存下杜绝并发推理 OOM |
| 推理失败 fallback mock | 链路永不中断，稳定性优先于单帧质量 |
