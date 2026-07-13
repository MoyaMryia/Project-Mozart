# Project Mozart · 音频 IO 与层间交互设计规范 (Audio IO & Inter-layer Spec)

> **版本**: v0.1.0  
> **定位**: 本文档定义了 Project Mozart 音频输入/输出（IO）子系统的架构、代码设计模式、内存缓冲策略以及预处理与后处理层的高性能交互规范。  
> **核心目标**: 极致性能、极低延迟、零运行时内存抖动，同时兼顾本地物理声卡和离线网络/网页端数据流的处理。

---

## 1. 系统架构与定位

Project Mozart 音频 IO 子系统负责打通从**物理输入设备/网络接收**到**预处理（C/Rust）**，再到**后处理推理（C++）**，最终到**物理输出设备/网络回传**的完整音频通路。

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                                 Project Mozart IO                               │
├─────────────────────────────────┬───────────────────────────────────────────────┤
│         实时流 (Real-Time)       │             离线/网页流 (Offline/Batch)        │
├─────────────────────────────────┼───────────────────────────────────────────────┤
│  · 物理硬件: PipeWire (Mic/Sink) │  · 网络数据: WebSocket / HTTP 离线文件上传    │
│  · 网络实时: UDP (定长契约包)    │  · 处理模式: 大块连续内存分片, 高吞吐         │
│  · 驱动方式: 20ms 帧驱动, 极低延迟│  · 内存策略: 自动按需分配与批量释放           │
└─────────────────────────────────┴───────────────────────────────────────────────┘
```

为了实现这一拓扑，IO 子系统划分为两大核心部分：
1. **音频流驱动抽象 (`AudioStream`)**：隔离底层硬件和协议差异，提供规范的设计模式。
2. **层间高性能通道 (`Inter-layer Channel`)**：连接预处理层与后处理层，支持预分配的本地 SPSC 环与网络 UDP 契约传输。

---

## 2. 核心契约与数据结构 (C-ABI 兼容)

为了使纯 C 编写的预处理层、C++ 编写的后处理层以及未来可能使用的 Rust 模块之间能够进行稳定、高效的内存交互，所有帧结构和元数据均采用严格字节对齐的 C-ABI 结构体定义。

### 2.1 16字节打包元数据 (`mozart_frame_meta_t`)
```c
#pragma pack(push, 1)
typedef struct {
    uint64_t pts_ns;       // 演示时间戳 (纳秒)，用于流式音频对齐
    uint32_t frame_idx;    // 单调递增的帧序号
    uint8_t  vad_flag;     // 语音激活标记: 0 = 静音 (跳过推理), 1 = 有声
    uint8_t  energy_db;    // 当前帧能量值 (dB, 0-255)
    uint8_t  conf;         // 降噪置信度 (0-255)
    uint8_t  segment_id;   // 声音分段 ID (0 = 间隔, 变化时重置流式状态)
} mozart_frame_meta_t;
#pragma pack(pop)
// 确保结构体严格占用 16 字节
```

### 2.2 48kHz 原始输入帧 (`mozart_raw_frame_t`)
与底层 PipeWire 物理采集输出对齐，帧长为 20 ms。`meta` 由 IO 模块在采集回调中填充（`pts_ns` 取系统单调时钟、`frame_idx` 单调递增、`vad_flag` 置 0 留待预处理判定），预处理与后处理只读不改 `pts_ns`/`frame_idx`。
```c
#define MOZART_RAW_SAMPLE_RATE    48000
#define MOZART_RAW_FRAME_MS       20
#define MOZART_RAW_SAMPLES        960 // 48000 * 0.02

#pragma pack(push, 1)
typedef struct {
    mozart_frame_meta_t meta;                     // 16B 侧带元数据
    float               pcm[MOZART_RAW_SAMPLES];  // 960 点 float32 单声道 PCM (3840 字节)
} mozart_raw_frame_t;
#pragma pack(pop)
// 帧大小: 16 + 3840 = 3856 字节
```

### 2.3 16kHz 输入契约帧 (`mozart_input_frame_t`)
与上游预处理输出对齐，帧长为 20 ms。
```c
#define MOZART_INPUT_SAMPLE_RATE  16000
#define MOZART_INPUT_FRAME_MS     20
#define MOZART_INPUT_SAMPLES      320 // 16000 * 0.02

#pragma pack(push, 1)
typedef struct {
    mozart_frame_meta_t meta;                     // 16B 侧带元数据
    float               pcm[MOZART_INPUT_SAMPLES]; // 320 点 float32 单声道 PCM (1280 字节)
} mozart_input_frame_t;
#pragma pack(pop)
// 帧大小: 16 + 1280 = 1296 字节
```

### 2.4 48kHz 输出契约帧 (`mozart_output_frame_t`)
与 RVC 变声生成器的原生输出对齐，帧长同样为 20 ms。
```c
#define MOZART_OUTPUT_SAMPLE_RATE 48000
#define MOZART_OUTPUT_FRAME_MS    20
#define MOZART_OUTPUT_SAMPLES     960 // 48000 * 0.02

#pragma pack(push, 1)
typedef struct {
    mozart_frame_meta_t meta;                      // 透传元数据
    float               pcm[MOZART_OUTPUT_SAMPLES]; // 960 点 float32 单声道 PCM (3840 字节)
} mozart_output_frame_t;
#pragma pack(pop)
// 帧大小: 16 + 3840 = 3856 字节
```

---

## 3. 音频流抽象与代码设计模式

为了提供规范的、可扩展的音频 IO 管理，设计采用 **策略模式 (Strategy Pattern)** 和 **工厂模式 (Factory Pattern)** 对音频流进行建模。

### 3.1 统一流接口 `AudioStream`
根据运行模式，音频流分为“实时帧驱动”和“离线批量驱动”两类。

```cpp
enum class StreamType {
    RealTime,  // 实时流（极低延迟，帧级驱动）
    Offline    // 离线流（批量处理，吞吐驱动，网页端 Socket 输入）
};

// 流方向：决定 ReadFrame/WriteFrame 期望的契约帧类型
enum class StreamDirection {
    Capture,   // 采集端：ReadFrame 产出 mozart_raw_frame_t (PipeWire) 或 mozart_input_frame_t (UDP 入)
    Playback   // 播放端：WriteFrame 消费 mozart_output_frame_t
};

// 打开流时的配置（由 status_manager 在编排时填充后下发给 IO 模块）
struct StreamConfig {
    StreamDirection direction;
    uint32_t        sample_rate;     // 期望采样率（采集流由设备决定，此处用于校验）
    uint32_t        frame_duration_ms = 20;
    uint32_t        ring_capacity;   // 内部预分配内存池帧数（实时流建议 16）
};

class AudioStream {
public:
    virtual ~AudioStream() = default;

    virtual StreamType     GetType() const noexcept = 0;
    virtual StreamDirection GetDirection() const noexcept = 0;
    virtual bool IsOpen() const noexcept = 0;

    // 打开流并预分配内存池（实时流零运行时 malloc 的前提）
    virtual bool Open(const StreamConfig& config) = 0;
    virtual void Close() = 0;
};
```

### 3.2 实时流接口 `RealTimeAudioStream`
物理音频（PipeWire）或实时网络包（UDP）继承此接口。数据传递通过固定尺寸的帧完成。

> **帧类型由流方向决定**：`Capture` 流的 `ReadFrame` 产出 `mozart_raw_frame_t`（PipeWire 物理采集，48kHz）或 `mozart_input_frame_t`（UDP 入，16kHz）；`Playback` 流的 `WriteFrame` 消费 `mozart_output_frame_t`（48kHz）。调用方按 `GetDirection()` 与具体子类约定分配缓冲区，`buf_size` 必须与该流期望的帧大小一致。

```cpp
class RealTimeAudioStream : public AudioStream {
public:
    StreamType GetType() const noexcept override { return StreamType::RealTime; }

    // 阻塞式读/写单帧音频数据（符合性能优先要求）
    // 帧类型由具体子类约定，buf_size 用于运行时校验
    virtual bool ReadFrame (void* out_frame_buf, uint32_t buf_size) = 0;
    virtual bool WriteFrame(const void* in_frame_buf, uint32_t buf_size) = 0;

    // 获取设备底层延迟 (单位: 纳秒)
    virtual uint64_t GetUnderlyingLatencyNs() const noexcept = 0;
};
```

### 3.3 离线/批量流接口 `OfflineAudioStream`
处理非实时模式下的网页音频流（如 WebSocket 上传的音频块、离线 WAV 文件转换）。

```cpp
class OfflineAudioStream : public AudioStream {
public:
    StreamType GetType() const noexcept override { return StreamType::Offline; }

    // 一次性读取/写入整块不规则音频 buffer
    virtual size_t ReadChunk (float* out_pcm, size_t max_samples, mozart_frame_meta_t& out_meta) = 0;
    virtual size_t WriteChunk(const float* in_pcm, size_t samples, const mozart_frame_meta_t& meta) = 0;
};
```

---

## 4. 底层驱动支持

```
              ┌───────────────────┐
              │    AudioStream    │
              └─────────┬─────────┘
                        │
         ┌──────────────┴──────────────┐
         ▼                             ▼
┌──────────────────┐         ┌────────────────────┐
│  RealTimeStream  │         │   OfflineStream    │
└────────┬─────────┘         └─────────┬──────────┘
         ├──────────────────┐          ├─────────────┐
         ▼                  ▼          ▼             ▼
┌──────────────┐     ┌───────────┐┌───────────┐ ┌──────────────┐
│ PipeWireStream│     │ UDPStream ││ FileStream │ │ SocketStream │
│ (本地物理声卡)│     │ (实时网络)││ (WAV文件)  │ │ (网页 WebSocket)
└──────────────┘     └───────────┘└───────────┘ └──────────────┘
```

1. **PipeWire 驱动 (`PipeWireStream`)**:
   - 绑定物理麦克风源 (Capture) 与虚拟输出源 (Virtual Source / Playback Sink)。
   - 使用 PipeWire 原生的实时共享环形缓冲区，将 PipeWire 的进程间延迟压缩在 **5ms** 内。
2. **UDP 实时驱动 (`UDPStream`)**:
   - 实现定长的 UDP 契约包收发。
   - 专门用于同机跨进程或跨机边缘部署的实时音频传输。
3. **网页/WebSocket 离线驱动 (`SocketOfflineStream`)**:
   - 监听 WebSocket 传来的非实时完整音频包。
   - 提取后送入后处理，按块处理并迅速将结果包发回网页端。
4. **Mock 测试驱动 (`MockAudioStream`)**:
   - 无需物理声卡 and 网络连接，直接读取测试用音频文件（如 `noisy_sample.wav`）并产生伪 20ms 定时帧。
   - 用于快速进行 CTest 闭环集成测试。

---

## 5. 内存管理与缓冲策略 (性能优先)

物理硬件 IO 线程最忌讳发生由于内存碎片和操作系统锁导致的运行期时延抖动（Jitter）。因此，本子系统实时流推行**零运行时动态内存分配**原则。

### 5.1 内存池预分配 (Memory Pooling)
- 在音频流打开（`Open()`）时，子系统预先分配足够数量的 `mozart_input_frame_t` 与 `mozart_output_frame_t` 结构体，存放在环形队列中。
- 环缓冲在打开阶段一次性分配；运行期 `push/pop` 只执行固定尺寸、有上界的帧拷贝与原子索引更新。
- 禁止在实时回调函数及推理循环内使用 `malloc`、`free`、`new`、`delete` 或是产生临时 `std::vector`。

### 5.2 离线流的连续缓存优化 (Batch Buffer Optimization)
- 离线流处理（如网页上传的整段音频）与实时流相反，注重**吞吐量**。
- 分配大块连续物理内存（大小在数兆字节左右，依据音频长度而定），使用分片方式输入，从而能够充分利用 CPU 的 L1/L2 缓存，提高特征提取和 RVC 推理的吞吐。

---

## 6. 多线程与同步模型 (实时性保证与解耦)

实时音频处理中，底层的 IO 读写具有极高优先级，绝对不能被推理耗时（通常在 10ms - 30ms 间波动）所阻塞。为此，我们设计了**两级无锁环形队列**的解耦方案。

```
                    ┌──────────────┐
                    │ 物理硬件采集 │ ── (PipeWire 实时回调线程, SCHED_FIFO)
                    └──────┬───────┘
                           │ (无锁单写单读环 - Input Device Ring)
                           ▼
                    ┌──────────────┐
                    │  预处理模块  │ ── (预处理 Worker 线程)
                    └──────┬───────┘
                           │ (层间无锁 SPSC RingBuffer)
                           ▼
                    ┌──────────────┐
                    │ 后处理推理器 │ ── (后处理 Worker 线程, GPU 推理)
                    └──────┬───────┘
                           │ (层间无锁 SPSC RingBuffer)
                           ▼
                    ┌──────────────┐
                    │ 物理虚拟输出 │ ── (PipeWire 实时播放线程, SCHED_FIFO)
                    └──────────────┘
```

### 6.1 双环异步解耦模式
我们放弃了“在音频 IO 回调中直接运行推理”的同步设计，转而采用**双环异步解耦模式**：
- **时钟隔离**：采集与播放设备按其硬件物理时钟（如 1024 样本/周期的中断）独立运行。采集线程只负责向“输入无锁环”写数据，播放线程只从“输出无锁环”读数据，不需要关心推理何时完成。
- **异步推理**：变声与预处理均在独立的 Worker 线程中工作。Worker 线程以阻塞/通知（`futex` 或条件变量）的方式监听输入环。一旦有 20ms 数据写入，Worker 立即苏醒，按顺序执行预处理（C ABI）与后处理（RVC 推理），然后推入输出无锁环。
- **线程优先级**：物理 IO 线程采用 `SCHED_FIFO` 实时调度策略，推理线程则使用常规调度策略或低优先级，保证即使 GPU 满载甚至挂起，硬件音频流（采集与播放）也不会被拖垮卡死。

### 6.2 单写单读无锁队列 (`SPSC Lock-Free RingBuffer`)
- 预处理与后处理的层间数据通道（同进程内）基于 **Single-Producer Single-Consumer (SPSC)** 无锁环形缓冲区实现。
- 基于 C++11 `std::atomic` 的原子操作管理 `write_index` 和 `read_index`。
- 在数据交互的临界区内，**绝对禁止**使用 `std::mutex` 或 `pthread_mutex`，防止 IO 线程因锁竞争被系统挂起（Priority Inversion，优先级反转），从而杜绝爆音（XRun）。

### 6.3 算法与推理逻辑解耦 (Algorithm-Agnostic Pipe)
为了保持 IO 系统的纯粹性和通用性，本 IO 模块被设计为**算法无关的通道（Algorithm-Agnostic Pipe）**。
- **职责分离**：如 RVC 变声中特征提取（HuBERT）和音高提取所需要的历史音频上下文（如 120ms 滑动窗口），属于**后处理推理引擎（Inference Layer）的内部逻辑**。
- **通道透明**：IO 模块对传输的数据内容不做任何算法解释。它只负责以固定契约帧（20ms）在预处理与后处理之间传输数据。推理引擎如果需要上下文，应在自己的 Worker 线程中自行构建滑动窗口。

### 6.4 超时积压丢帧追赶机制 (Jump/Drop Recovery)
若 GPU 偶尔因系统繁忙（如温度墙降频、并发冲突）导致推理耗时瞬时从 10ms 飙升至 30ms，输入环中会发生音频帧积压。如果不进行干预，积压产生的额外延迟将永远留在队列中，导致通话滞后（变声比说话慢半拍）。

为此我们设计了**丢帧追赶恢复机制**：
- **阈值检测**：Worker 线程在处理前，首先检查输入无锁环中未处理的音频帧积压数量 $N$。
- **丢帧追赶 (Drop Oldest)**：若 $N > 4$（即积压超过 80ms），说明发生了延迟累积。Worker 线程会**立刻跳过并丢弃（Drop）最旧的 $N-1$ 帧**，仅对最新的一帧进行变声推理。被丢弃的帧，系统会自动向播放缓冲区写入全零的静音帧或提供原声直通作为平滑过渡。
- **效果**：在丢帧瞬间，用户可能会听到极其短暂的一下微小断续（卡顿），但系统能够**在 20ms 内瞬间把累积延迟清零**，保证变声的长久实时性。

### 6.5 爆音 (XRun) 隔离保护机制
- 当后处理推理线程因为 GPU 瞬时波动超时，输出无锁环中没有新的音频数据时，物理输出线程（PipeWire Stream）自动**回填静音数据（即全零的 float32 数据帧）**。
- 该设计能防止音频驱动层因为没有数据读取而抛出 XRun 异常，保证系统的稳定运行。同时系统会触发统计警报。

---

## 7. 模块交付与接口暴露规范 (API Specification)

为了满足“极致性能的实时处理”与“免底层开发的网页/脚本接入”两类场景，Project Mozart IO 子系统采用**混合交付形态**。

### 7.1 本地 C-ABI 动态链接库接口 (`mozart/audio_io.h`)
供 C/C++/Rust 开发者直接链接并调用，获取无运行时分配的固定帧流与 SPSC 环接口。

```c
#ifndef MOZART_AUDIO_IO_H
#define MOZART_AUDIO_IO_H

#include <stdint.h>
#include <stdbool.h>
#include "mozart/frame_meta.h"   // 契约帧 + 元数据唯一定义源

#ifdef __cplusplus
extern "C" {
#endif

// ==========================================
// 1. 音频流生命周期管理 (物理/网络 IO)
// ==========================================
typedef void* mozart_stream_handle_t;

// 流方向：与 StreamConfig.direction 对齐
#define MOZART_IO_DIR_CAPTURE  0   // 采集端：ReadFrame 产出 raw_frame 或 input_frame
#define MOZART_IO_DIR_PLAYBACK 1   // 播放端：WriteFrame 消费 output_frame

// 创建物理硬件实时流 (PipeWire 麦克风采集或虚拟源输出)
// device_name: 设备标识 (如 "default", "mozart_mic")
// direction:   MOZART_IO_DIR_CAPTURE = 采集, MOZART_IO_DIR_PLAYBACK = 播放
// 采集流 ReadFrame 期望 mozart_raw_frame_t (48kHz / 3856B)
// 播放流 WriteFrame 期望 mozart_output_frame_t (48kHz / 3856B)
mozart_stream_handle_t mozart_io_create_pipewire_stream(const char* device_name, int direction);

// 创建实时网络 UDP 流 (定长契约包收发)
// host:      本地绑定地址（Capture）或对端地址（Playback）
// port:      UDP 端口
// direction: MOZART_IO_DIR_CAPTURE = 接收 input_frame (16kHz / 1296B)
//            MOZART_IO_DIR_PLAYBACK = 发送 output_frame (48kHz / 3856B)
mozart_stream_handle_t mozart_io_create_udp_stream(const char* host, uint16_t port, int direction);

// 构造与打开分离，便于 status_manager 保留配置并独立启停底层资源
bool mozart_io_open_stream(mozart_stream_handle_t handle,
                           uint32_t sample_rate,
                           uint32_t frame_duration_ms,
                           uint32_t ring_capacity);
void mozart_io_close_stream(mozart_stream_handle_t handle);
bool mozart_io_is_stream_open(mozart_stream_handle_t handle);

// 关闭并销毁音频流
void mozart_io_destroy_stream(mozart_stream_handle_t handle);

// 阻塞式读写 20ms 契约帧 (内部使用内存池，零运行时 malloc)
// buf_size 必须与具体流期望的帧大小一致（见上方各工厂函数的注释）
bool mozart_io_read_frame (mozart_stream_handle_t handle, void*       out_frame_buf, uint32_t buf_size);
bool mozart_io_write_frame(mozart_stream_handle_t handle, const void* in_frame_buf, uint32_t buf_size);


// ==========================================
// 2. 无锁环形缓冲区接口 (SPSC Lock-free RingBuffer)
// ==========================================
typedef void* mozart_ring_handle_t;

// 创建无锁单写单读环形队列
// capacity: 可容纳的最大帧数
// item_size: 每一帧的数据大小 (字节数)
mozart_ring_handle_t mozart_ring_create(uint32_t capacity, uint32_t item_size);

// 销毁队列
void mozart_ring_destroy(mozart_ring_handle_t ring);

// 向队列写入数据 (SPSC 模式，由 Producer 线程调用)
bool mozart_ring_push(mozart_ring_handle_t ring, const void* data);

// 从队列读出数据 (SPSC 模式，由 Consumer 线程调用)
bool mozart_ring_pop(mozart_ring_handle_t ring, void* out_data);

// 获取队列中当前可供读取的帧数
uint32_t mozart_ring_get_readable_count(mozart_ring_handle_t ring);

#ifdef __cplusplus
}
#endif

#endif // MOZART_AUDIO_IO_H
```

### 7.2 跨语言动态加载契约 (C-ABI 预处理调用)
后处理引擎 (C++) 可以通过 `dlopen` 预处理共享库，获取预处理的核心控制接口：
```cpp
extern "C" {
    // 初始化预处理环境，传入配置结构体，返回不透明上下文指针
    mozart_pre_ctx_t* mozart_pre_init(const mozart_pre_config_t* cfg);
    
    // 预处理主循环：由后处理 Worker 调用。
    // 传入原始采集数据 (48kHz f32, 960 samples)，处理结果直接输出到 clean_output (16kHz f32, 320 samples)，并填充 16B 元数据
    int mozart_pre_process(mozart_pre_ctx_t* ctx,
                           const float* in, int in_samples,
                           float out[MOZART_INPUT_SAMPLES],
                           mozart_frame_meta_t* meta);
    
    // 释放预处理资源
    void mozart_pre_free(mozart_pre_ctx_t* ctx);
}
```

### 7.3 网页/非实时网络 Daemon 接口 (WebSocket & HTTP)
针对网页前端（非实时文件上传）及脚本调用，不要求极致的帧级微秒级延迟，后台启动 Daemon 进程暴露以下两个端点：

#### 7.3.1 HTTP 离线文件变声端点
* **端点**: `POST /api/v1/voice_changer/offline`
* **Content-Type**: `multipart/form-data`
* **请求参数**:
  - `file`: 待变声的音频文件（WAV / MP3，前端直接录制完毕后上传的完整大块音频）。
  - `model_id`: 目标变声 RVC 模型的 ID (字符串)。
* **处理机制**:
  - 后端直接拉入 `OfflineAudioStream`，按大块内存载入。
  - 多线程分片并行做预处理和特征提取。
  - 送入 RVC 推理完成后，合并生成完整的 WAV 文件。
* **返回**: `audio/wav` 二进制音频文件流。

#### 7.3.2 WebSocket 实时控制与非实时流交互
用于网页端建立流式数据连接。
* **端点**: `WS /api/v1/voice_changer/ws`
* **交互流程**:
  1. **建连与控制**: 客户端连接后发送 JSON 控制帧进行配置（如选定 RVC 变声模型、控制音调等）。
     ```json
     { "event": "config", "model_id": "target_model_01", "pitch_shift": 12 }
     ```
  2. **音频块推流**: 前端采集到大块音频数据后（不强求 20ms，通常是按页大小如 4096 字节的 PCM 二进制流），直接以 **Binary Message** 格式通过 WebSocket 推送。
  3. **分片处理**: 后端将其缓存于 `OfflineAudioStream` 的 Batch 连续内存中，按分片喂给 RVC 引擎推理。
  4. **变声回传**: 变声完成后，后端生成 48kHz 的 float32 PCM 数据，以 **Binary Message** 格式推回前端网页进行渲染播放。

### 7.4 典型调用链集成示例 (C-ABI Integration Example)

下面的示例展示了与 §6 双环图完全对齐的**四线程解耦架构**：采集 → 预处理（独立线程）→ 推理 → 播放，三个 SPSC 无锁环串联。这是本地物理设备完整链路（UDP 网络链路时，预处理位于发端，接收端直接从 UDP Capture 流读 input_frame 进推理环）。

```c
// 示例：演示如何通过 C-ABI 接口进行四线程异步变声（对齐 §6 双环图）
#include "mozart/audio_io.h"
#include "mozart.h"              // mozart_pre_* 的配置/原型
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

// 全局无锁环形队列句柄（三环串联：raw → input → output）
static mozart_ring_handle_t raw_ring;     // 采集 → 预处理（承载 mozart_raw_frame_t）
static mozart_ring_handle_t input_ring;   // 预处理 → 推理  （承载 mozart_input_frame_t）
static mozart_ring_handle_t output_ring;  // 推理  → 播放   （承载 mozart_output_frame_t）

// 1. 音频采集线程 (实时线程, 优先级 SCHED_FIFO)
void* capture_thread_fn(void* arg) {
    // 实例化物理硬件采集流 (PipeWire 麦克风, 采集 48kHz 原始音频)
    mozart_stream_handle_t mic = mozart_io_create_pipewire_stream("default_mic", MOZART_IO_DIR_CAPTURE);
    mozart_io_open_stream(mic, MOZART_RAW_SAMPLE_RATE, MOZART_RAW_FRAME_MS, 16);
    mozart_raw_frame_t frame;
    while (mozart_io_read_frame(mic, &frame, sizeof(frame))) {
        // 写入失败（环满）直接跳过以丢帧，防止阻塞物理采集
        mozart_ring_push(raw_ring, &frame);
    }
    mozart_io_destroy_stream(mic);
    return NULL;
}

// 2. 预处理 Worker 线程 (独立线程，对应 §6 图中的"预处理 Worker")
void* preprocess_thread_fn(void* arg) {
    mozart_pre_config_t cfg = {0};
    mozart_pre_ctx_t* pre_ctx = mozart_pre_init(&cfg);

    mozart_raw_frame_t    raw_frame;
    mozart_input_frame_t  in_frame;
    while (true) {
        if (!mozart_ring_pop(raw_ring, &raw_frame)) continue;
        // 48kHz / 960 点 → 16kHz / 320 点，元数据由预处理填充
        mozart_pre_process(pre_ctx,
                           raw_frame.pcm, MOZART_RAW_SAMPLES,
                           in_frame.pcm, &in_frame.meta);
        mozart_ring_push(input_ring, &in_frame);
    }
    mozart_pre_free(pre_ctx);
    return NULL;
}

// 3. 推理 Worker 线程 (后处理变声，GPU 推理)
void* infer_thread_fn(void* arg) {
    mozart_input_frame_t   in_frame;
    mozart_output_frame_t  out_frame;

    // 算法上下文缓冲区 (16kHz f32, 120ms = 1920 采样点上下文)
    // [注]: 滑动窗口维护属于后处理推理层的内部职责，IO 模块对其透明
    float rvc_sliding_window[1920] = {0.0f};

    while (true) {
        // 超时积压判定：丢帧追赶，仅保留最新一帧
        uint32_t pending = mozart_ring_get_readable_count(input_ring);
        if (pending > 4) {
            for (uint32_t i = 0; i < pending - 1; ++i)
                mozart_ring_pop(input_ring, &in_frame);  // 弃最旧 N-1 帧
            printf("[Warn] Latency build-up! Dropped %u old frames.\n", pending - 1);
        }

        if (!mozart_ring_pop(input_ring, &in_frame)) continue;

        // 推理层自行维护滑动窗口：移出最旧 20ms，追加新 20ms
        memmove(rvc_sliding_window, rvc_sliding_window + MOZART_INPUT_SAMPLES,
                (1920 - MOZART_INPUT_SAMPLES) * sizeof(float));
        memcpy(rvc_sliding_window + 1920 - MOZART_INPUT_SAMPLES,
               in_frame.pcm, MOZART_INPUT_SAMPLES * sizeof(float));

        // 运行 RVC 推理（C++ 后处理层暴露的接口），输出 48kHz / 960 点
        out_frame.meta = in_frame.meta;
        rvc_infer_20ms(rvc_sliding_window, out_frame.pcm);

        mozart_ring_push(output_ring, &out_frame);
    }
    return NULL;
}

// 4. 音频输出播放线程 (实时播放线程, 优先级 SCHED_FIFO)
void* playback_thread_fn(void* arg) {
    mozart_stream_handle_t spk = mozart_io_create_pipewire_stream("virtual_sink", MOZART_IO_DIR_PLAYBACK);
    mozart_io_open_stream(spk, MOZART_OUTPUT_SAMPLE_RATE, MOZART_OUTPUT_FRAME_MS, 16);
    mozart_output_frame_t frame;
    while (true) {
        if (mozart_ring_pop(output_ring, &frame)) {
            mozart_io_write_frame(spk, &frame, sizeof(frame));
        } else {
            // 爆音保护：输出环为空时回填静音帧，防止声卡 XRun
            mozart_output_frame_t silence = {0};
            mozart_io_write_frame(spk, &silence, sizeof(silence));
        }
    }
    mozart_io_destroy_stream(spk);
    return NULL;
}

int main(void) {
    // 三环：raw(3856B) → input(1296B) → output(3856B)
    raw_ring    = mozart_ring_create(16, sizeof(mozart_raw_frame_t));
    input_ring  = mozart_ring_create(16, sizeof(mozart_input_frame_t));
    output_ring = mozart_ring_create(16, sizeof(mozart_output_frame_t));

    // 创建各线程（采集/播放 SCHED_FIFO，预处理/推理 常规优先级）...
    // (此处略去 pthread_create 代码)
    return 0;
}
```

---

## 8. 设计模式总结

1. **策略模式 (Strategy)**:
   - `AudioStream` 为核心策略接口，`PipeWireStream`、`UDPStream` 等为具体的策略实现。高层逻辑通过配置动态装载策略，降低层与底层驱动的耦合。
2. **预分配有界拷贝 (Preallocated Bounded Copy)**:
   - SPSC 环在打开阶段一次性分配，运行期仅执行固定帧 memcpy 与原子索引更新，不产生堆分配抖动。
3. **防弹保护模式 (Fail-safe)**:
   - GPU 耗时失控或死锁时，IO 输出端通过全零静音填充，保护底层的 PipeWire 引擎不崩溃、不爆音。
4. **通道透明与丢帧自适应机制 (Adaptive Buffer & Agnostic Pipe)**:
   - 通道上保持绝对算法透明，仅在调度上采用丢帧机制主动对抗 GPU 延迟波动，确保端到端极致实时性。
5. **混合同步异步交付 (Hybrid API)**:
   - 在底座层暴露出纯净 C-ABI 头文件接口，在服务层包裹出易于现代网页及脚本开发的 HTTP/WS 服务，完成多层级调用的闭环。

---

## 9. 预期的文件系统结构 (Directory & File Layout)

为了实现以上规范，`IO/` 目录结构规划如下：

```
IO/
├── README.md                      # 音频 IO 与层间交互设计规范（本文）
├── CMakeLists.txt                 # IO 模块构建脚本（产出 libmozart_io 共享库）
├── include/                       # 头文件目录（对外暴露）
│   └── mozart/
│       ├── frame_meta.h           # 16B 元数据 + raw/input/output 契约帧（C-ABI，唯一定义源）
│       ├── audio_io.h             # 统一 C-ABI 接口（流工厂 + 无锁环）
│       ├── audio_stream.hpp       # C++ 流抽象类（AudioStream / RealTimeAudioStream / OfflineAudioStream）
│       ├── ring_buffer.hpp        # SPSC 无锁环形队列（C++ 接口，audio_io.h 的 C-ABI 由其实现）
│       ├── pipewire_stream.hpp    # PipeWire 本地物理设备驱动（C++）
│       ├── udp_stream.hpp         # 实时网络 UDP 契约包驱动（C++）
│       ├── file_stream.hpp        # WAV 文件离线驱动（C++）
│       └── mock_stream.hpp        # Mock 测试驱动（C++，无物理设备/网络）
├── src/                           # 实现文件目录
│   ├── audio_stream.cpp           # 统一音频流基类 / 工厂分发
│   ├── ring_buffer.cpp            # C-ABI mozart_ring_* 实现 + C++ SpscRing
│   ├── pipewire_stream.cpp        # PipeWire 驱动实现
│   ├── udp_stream.cpp             # UDP 驱动实现
│   ├── file_stream.cpp            # WAV 文件驱动实现
│   ├── mock_stream.cpp            # Mock 驱动实现
│   └── daemon/                    # 网页/非实时 Daemon 后台服务
│       ├── main.cpp               # Daemon 启动入口
│       ├── http_service.cpp       # HTTP 离线文件上传转换服务
│       └── ws_service.cpp         # WebSocket 网页流式交互服务
└── tests/                         # 单元与集成测试目录
    ├── CMakeLists.txt             # 测试构建配置
    ├── test_ring_buffer.cpp       # 无锁环功能与性能测试
    └── test_mock_stream.cpp       # 基于 Mock 音频文件的无物理设备测试
```

> **契约帧唯一定义源**：`frame_meta.h` 是 `mozart_frame_meta_t` / `mozart_raw_frame_t` / `mozart_input_frame_t` / `mozart_output_frame_t` 的唯一定义源。`preprocessor/mozart.h` 与 `rvc-backend` 的 FrameMeta 均改为 `#include "mozart/frame_meta.h"`，消除三处各自定义的同步漂移风险。
