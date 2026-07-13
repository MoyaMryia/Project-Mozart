# Project Mozart 多模式状态机与内存管理设计规范

本规范定义了 Project Mozart 后端支持 **特定人声变声（如 RVC） / 零样本提示词变声（Zero-Shot VC，如 Seed-VC、CosyVoice 等）** 以及 **实时（Stream） / 音频文件（File）** 的 4 种工作模式下的状态机设计、显存置换策略与 API 契约。

---

## 1. 状态空间与维度解耦

我们采用 **正交解耦（Decoupled Axes）** 设计，将 4 种具体模式拆分为两个核心维度，这有利于后端管线逻辑的模块化与灵活扩展：

```
                    ┌─────────────── Model Type ───────────────┐
                    │      RVC (ONNX)     │    Zero-Shot (Torch/ONNX)  │
┌───────┬───────────┼─────────────────────┼────────────────────────┤
│       │  Stream   │     ① RT_RVC        │       ③ RT_ZERO_SHOT   │
│  I/O  │ (PipeWire)│ (麦克风直连扬声器)  │  (实时零样本变声)      │
│ Type  ├───────────┼─────────────────────┼────────────────────────┤
│       │   File    │     ② FILE_RVC      │       ④ FILE_ZERO_SHOT │
│       │  (Multi)  │ (上传音频批量转换)  │  (上传音频零样本转换)  │
└───────┴───────────┴─────────────────────┴────────────────────────┘
```

---

## 2. 状态转移与强互斥生命周期

为了在 **Jetson Orin Nano 8GB 共享内存** 环境下达到极致稳定性，我们设计了 **强互斥状态机（Exclusive State Machine）**。在任意时刻，系统仅允许一个工作状态处于 `ACTIVE`：

```mermaid
state-diagram-v2
    [*] --> IDLE
    
    state IDLE {
        note right of IDLE : 设备空闲，显存中保留上次加载的模型
    }

    IDLE --> RT_RVC : Switch(RT_RVC)
    IDLE --> FILE_RVC : Switch(FILE_RVC)
    IDLE --> RT_ZERO_SHOT : Switch(RT_ZERO_SHOT)
    IDLE --> FILE_ZERO_SHOT : Switch(FILE_ZERO_SHOT)

    state RT_RVC {
        note : 独占物理麦克风/扬声器\n加载 HuBERT+RMVPE+Generator
    }

    state FILE_RVC {
        note : 释放物理音频设备\n仅加载 RVC 核心进行文件批处理
    }

    RT_RVC --> FILE_RVC : 1. 释放设备\n2. 保持模型常驻
    FILE_RVC --> RT_ZERO_SHOT : 1. 卸载 RVC 显存\n2. 垃圾回收 (GC)\n3. 启动设备\n4. 加载 Zero-Shot 模型
```

### 2.1 核心生命周期动作
1. **切换到 File 模式**：后端线程将主动调用 `PipeWire` 释放底层物理捕获与播放设备（`pw_stream_disconnect`），这既保障了用户隐私（麦克限制），也杜绝了硬件设备的独占冲突。
2. **跨模型大类切换**（RVC ↔ Zero-Shot）：为了防止 8GB 显存溢出（OOM），切换时显式执行 `UnloadModel()`、重置 TensorRT/ONNX Runtime 句柄，并调用垃圾回收器（Python `gc.collect()` 或 C++ `cudaDeviceReset` 部分重置）。
3. **同类模式切换**（如 `RT_RVC` ↔ `FILE_RVC`）：**不触发模型重载**，仅重定向 I/O 流对象（FileSource vs StreamSource），保持亚毫秒级的切换体验。

### 2.2 状态切换队列与优雅等待机制 (State Transition Queue & Graceful Waiting)

为了防止模式切换中断正在运行的批量文件转换（造成文件损坏或未完成写入），我们引入了“立即切换”与“排队等待切换”的双轨控制逻辑：

#### 2.2.1 切换触发机制

| 当前激活状态 (Current State) | 目标状态 (Target State) | 切换逻辑 (Transition Logic) | 行为描述 (Behavior) |
|---|---|---|---|
| **实时模式 (`RT_*`)** | 任意其他状态 | **立即切换 (Immediate)** | 1. 立即中断实时音频流 (Capture/Playback)<br>2. 释放 PipeWire 设备<br>3. 卸载显存（如需）并执行目标状态初始化 |
| **非实时模式 (`FILE_*`)**<br>(有任务正在运行) | 任意其他状态 | **优雅等待 (Graceful Deferred)** | 1. 将切换请求存入内存“待处理切换槽 (Pending Switch Slot)”<br>2. 当前音频文件转换任务**继续运行**直到完成并写入 EOF<br>3. 转换线程检测到 EOF 并关闭文件后，自动触发待处理切换槽中的目标状态切换<br>4. 执行目标模式初始化 |
| **非实时模式 (`FILE_*`)**<br>(无任务运行/空闲) | 任意其他状态 | **立即切换 (Immediate)** | 1. 此时任务队列为空，没有活动线程<br>2. 立即执行目标模式切换 |

#### 2.2.2 状态切换队列控制流程

1. **指令接收**：当用户请求 `/api/mode/switch` 时，API 控制器检查当前系统状态指示器 `current_state` 及工作线程忙碌标志 `is_worker_busy`。
2. **状态拦截**：
   * 如果 `current_state` 为 `FILE_*` 且 `is_worker_busy == true`：API 拦截器将目标状态写入内存全局原子变量 `pending_target_state`，并对 API 请求立即返回：
     ```json
     {
       "status": "switching_deferred",
       "message": "当前有文件正在转换，转换完成后将自动为您切换至目标模式",
       "current_active_job": "job_10329048",
       "target_mode": "rt_rvc"
     }
     ```
   * 否则，立即执行切换动作，返回 `switching_complete`。
3. **完成回调与状态激活**：当非实时批量转换任务执行完毕时，后台工作线程触发“工作结束”钩子，执行如下抽象判定：
   - 检查 `pending_target_state` 全局槽是否为空。
   - 若不为空，提取并重置该槽中的目标模式。
   - 调用系统状态转换引擎，执行真实切换（释放资源并装载新设备/新模型大类）。

### 2.3 IO 编排边界

`status_manager` 不直接调用 PipeWire、socket 或预处理内部实现，只编排两个稳定边界：

1. **IO 生命周期门面**（`IO/include/mozart/audio_io.h`）：`mozart_io_create_*`、`mozart_io_open_stream`、`mozart_io_close_stream`、`mozart_io_destroy_stream`。
2. **业务 Worker 门面**：实时 RVC 当前由 `rvc::AudioWorker::start/stop` 提供；后续 Zero-Shot Worker 应保持同样的启停语义。

实时模式切换顺序固定为：

```text
stop current worker
  -> close current IO stream(s)
  -> unload/load model resources when model family changes
  -> open target IO stream(s)
  -> start target worker
```

`FILE_*` 模式不复用实时流：进入文件模式前先关闭 PipeWire/UDP 实时流；文件流适配器尚未实现，后续应继续放在 `IO/`，不能回填到预处理或推理模块。

---

## 3. 显存置换策略 (Smart Swapping)

RVC 包含多个子网络（HuBERT 编码器、RMVPE 音高检测器、角色生成器 Generator）。为了优化加载时间，显存置换策略细化如下：

| 转换路径 | 显存置换行为 | 加载耗时（估算） |
|---|---|---|
| RVC 角色 A ➔ RVC 角色 B | 仅重载并替换 Generator 节点；常驻 HuBERT & RMVPE | ~200 ms |
| RVC ➔ Zero-Shot VC | 释放整个 RVC (ONNX) 实例；加载 Zero-Shot (Torch/ONNX) 模型 | 3.0 ~ 5.0 s |
| Zero-Shot VC ➔ RVC | 释放 Zero-Shot 实例；加载 HuBERT + RMVPE + 默认 RVC 角色 | 4.0 ~ 6.0 s |

---

## 4. 双通道算力优先级设计

即便在强互斥状态下，在“实时变声运行中，允许后台上传并排队转换音频文件”的边缘情况下，系统通过以下方案保障实时音轨不发生 Underrun 卡顿：

1. **CPU 调度**：实时流处理线程设置高实时优先级（如 `SCHED_FIFO`，Priority=90），文件批量转换线程以低 CPU 优先级（`nice 19`）运行。
2. **GPU 调度 (CUDA Streams)**：
   * **实时推理** 提交到高优先级 CUDA Stream：`cudaStreamCreateWithPriority(&stream_rt, 0, -1)`。
   * **文件转换** 提交到低优先级 CUDA Stream：`cudaStreamCreateWithPriority(&stream_file, 0, 0)`。
   * 这保证了即使文件处理并发提交，GPU 也会硬件级抢占（Preempt）执行实时音频样本的核函数。

---

## 5. 多格式音频解码适配 (Audio Decoding Adapter)

为了支持用户上传多样化的音频格式（如 MP3、M4A、AAC、FLAC、OGG、WAV 等），后端 API Gateway 处集成了解码层。在将音频送入推理管线前，进行自适应重采样与声道合并：

```
                    ┌───────────────── 后端入口 ─────────────────┐
[ 任意音频格式上传 ] ➔ [ 音频解码层 (FFmpeg / libav) ] ➔ [ 转换为 16kHz Mono f32 ]
                                                                     │
                                                                     ▼
                                                          [ 送入 RVC/Zero-Shot VC 管线 ]
```

---

## 6. 零样本变声角色配置文件注册 (Speaker Profile)

Zero-Shot VC（如 Seed-VC 等）作为零样本提示词变声器，依赖 Target Voice（目标人声）的 Prompt Audio。我们设计了**统一角色配置文件注册表**：

```
                      ┌────────────────────────────────────────┐
                      ▼                                        │
[ 用户 Web 上传 alice.wav ] ➔ [ 后端提取 Embedding 固化 ] ➔ [ 生成 Profile alice ]
                                                               │
                                                               ├─➔ 用于 RT_ZERO_SHOT (带alice ID)
                                                               └─➔ 用于 FILE_ZERO_SHOT (带alice ID)
```

通过这一设计，用户切换或运行模式时，无需重复传递提示音频，仅需在 API 中传递 `speaker_id` 即可。

---

## 7. 前端控制 Web API 契约

后端将暴露以下核心 HTTP 接口，以供前端进行状态切换及任务分发：

### 7.1 切换系统模式
* **接口**：`POST /api/mode/switch`
* **请求体**：
```json
{
  "mode": "rt_rvc",          // 模式：rt_rvc | file_rvc | rt_zero_shot | file_zero_shot
  "speaker_id": "alice",     // 角色 ID (用于 RVC 或 Zero-Shot VC)
  "options": {
    "device": "cuda",
    "index_rate": 0.3
  }
}
```
* **逻辑**：
  1. 校验目标 `mode` 对应的模型是否已加载。若为同一大类模型，仅重置 I/O。
  2. 若模型大类变更，先释放旧模型显存，再异步加载新模型。
  3. 重建 I/O 链路（激活/断开 PipeWire 设备）。

### 7.2 注册角色声音配置 (提示人声输入)
* **接口**：`POST /api/speaker/register`
* **请求格式**：`multipart/form-data`
  * `name`: "alice"
  * `audio_file`: (提示人声音频文件，支持常见格式，5-10秒)
* **响应**：
```json
{
  "speaker_id": "alice",
  "status": "ready",
  "embedding_size": 256
}
```

### 7.3 异步音频文件转换
* **接口**：`POST /api/file/convert`
* **请求格式**：`multipart/form-data`
  * `audio_file`: (需要变声的源音频文件，支持 MP3/M4A/FLAC/WAV/AAC 等)
  * `speaker_id`: "alice"
  * `model_type`: "rvc"  // rvc | zero_shot
  * `output_format`: "wav" // wav | mp3 (自适应选择输出文件编码)
* **响应** (立即返回 Job ID)：
```json
{
  "job_id": "job_10329048",
  "status": "queued"
}
```
* 用户随后通过 `GET /api/file/status?job_id=job_10329048` 查询进度并下载结果。在强互斥状态下，若当前正处于 `RT_RVC` 或 `RT_ZERO_SHOT` 状态，该转换作业将被标记为挂起（Queued），直到进入 `IDLE` 或 `FILE` 状态才开始在后台进行消费。

---

## 8. 任务队列设计规范 (Task Queue Specification)

为了支持批量音频文件转换并防止并发推理导致系统过载，后端需要维持一个轻量级的**内存任务队列（In-Memory FIFO Queue）**。

### 8.1 为什么需要任务队列？
1. **显存与算力保护**：Jetson Orin Nano 的 8GB LPDDR5 共享显存无法支撑多通道并发推理。通过队列串行处理（串行度为 1），可保证系统不会发生 OOM 崩溃，并让单次推理获得最大算力。
2. **连接解耦**：用户上传大文件时，如果 HTTP 连接一直挂起等待推理完成，在弱网下极易超时断连。采用“立即响应 Job ID ➔ 后台排队 ➔ 前端轮询状态”的模式更为稳健。
3. **实时模式让步**：当系统处于实时流变声状态（`RT_*`）时，队列进入**暂停消费（Paused）**状态。新提交的文件任务在队列中积压排队，不抢占任何 CPU/GPU 算力，从而完全保障实时通话体验。当用户主动切出实时模式时，队列自动恢复消费（Resumed）。

### 8.2 任务生命周期与状态定义

```
         [ POST /api/file/convert ]
                     │
                     ▼
                 ┌───────┐
                 │QUEUED │ ➔ 处于队列中排队，若当前处于实时模式则暂停消费
                 └───┬───┘
                     │
                     ▼ (轮到该任务且处于非实时模式)
               ┌───────────┐
               │PROCESSING │ ➔ 音频开始解码 ➔ 送入管线推理 ➔ 编码输出
               └─────┬─────┘
                     ├───────────────────┐
                     ▼ (成功)            ▼ (出错/超时)
                ┌─────────┐          ┌────────┐
                │COMPLETED│          │ FAILED │
                └─────────┘          └────────┘
```

- **任务实体（Job Entity）**包含以下核心属性：
  - `job_id`: 唯一标识。
  - `status`: `queued` | `processing` | `completed` | `failed`。
  - `progress`: 进度百分比（可通过 `已处理帧数 / 总帧数` 计算）。
  - `source_file_path`: 暂存在本地的上传原始音频路径。
  - `output_file_path`: 处理成功后生成的目标音频路径，供前端拉取下载。
  - `error_message`: 任务失败时的错误日志。

### 8.3 队列实现建议
- **无重度中间件依赖**：由于后端部署在单台 Jetson Orin Nano 上，**无需**引入 Redis、RabbitMQ 等复杂的分布式中间件。
- **技术栈实现**：
  - 若选用 Python，可使用 `asyncio.Queue` 进行并发排队。
  - 若选用 C++，可使用互斥锁与条件变量（Mutex & Condition Variable）保护的 `std::queue`。
  - 队列深度（最大容纳任务数）建议限制在 50，超出后直接拒绝新请求，以防本地磁盘空间被上传的音频文件塞满。

---

## 9. 缓存文件系统与阈值清理机制 (File Storage & Threshold Eviction)

为了在无用户系统的极简模式下，安全、高效地管理音频文件，我们设计了**时间戳重命名 + 扁平单文件夹 + 同步阈值逐退**的文件管理策略。

### 9.1 存储目录与文件命名约定

系统在本地仅维持一个统一的临时缓存文件夹 `storage/temp/`（在项目根目录下）：

1. **命名规范**：所有文件使用**微秒级 Unix 时间戳 (Timestamp)** 进行命名，以防止并发上传时的重名冲突，同时实现文件名天然的按时间先后排序：
   - **上传的原始音频**：`[timestamp]_input.[ext]`（例如：`1719884800239_input.mp3`）
   - **变声后的输出音频**：`[timestamp]_output.[ext]`（例如：`1719884800239_output.wav`）
2. **免除用户系统**：由于现阶段无用户系统，后端生成唯一的 `timestamp` 写入文件名作为文件锁与 `job_id`，立即返回。

### 9.2 同步检查与阈值清理算法 (Post-job Eviction)

每次**音频文件写入磁盘成功（变声完成）**或**有新文件成功上传**时，后端工作线程同步触发空间大小审计和逐退清理逻辑：

1. **高水位阈值 (High Watermark)**：可配置的缓存文件夹容量限制（默认：`MAX_CACHE_SIZE_MB = 1000`，即 1GB）。
2. **低水位目标 (Low Watermark)**：清理的目标水位线（默认：高水位阈值的 80%，即 800MB），防止每次生成新文件都高频触发删除操作。
3. **逐退驱逐流程**：
   - 遍历 `storage/temp/` 统计当前占用的总字节数。
   - 若总字节数超过高水位阈值，获取该目录下的文件列表。
   - **字母顺序排序（Alphabetical Sort）**：由于文件名前缀为时间戳，按文件名升序排列即等价于从旧到新排序（免去了在 SD 卡上频繁读取 `stat` 修改时间的 I/O 开销）。
   - 从列表头部依次删除最旧的文件（包括对应的输入和输出音频），并重新累加文件夹大小。
   - 当容量降低至低水位目标以下时，停止删除，释放锁并结束清理。

---

*Generated 2026-07-12 · Project Mozart Backend Architecture Group*

