# Project Mozart · 状态管理与资源编排器 (state_manager)

`state_manager` 模块负责 Project Mozart 后端的全局生命周期管理、I/O 设备重定向、显存/模型智能置换以及多线程算力调度编排。

其核心设计目标是在 NVIDIA Jetson Orin Nano (8GB VRAM) 共享内存架构下实现**极高吞吐、极低延迟以及不爆音 (Fail-safe) 的强互斥系统稳定性**。

---

## 🔗 系统交互与交互式架构图

我们为系统设计了直观的**交互式系统调用链与拓扑图**。该网页展示了系统数据流、线程解耦、SPSC/UDP层间传输规范以及编排层面的时序控制：

* 📄 **交互式架构面板**：[architecture.html](file://./architecture.html) (可直接使用浏览器打开查看)

---

## 1. 核心编排设计：控制面与数据面解耦

状态机遵循 **控制面与数据面解耦（Control-Data Plane Separation）** 的架构原则：
* **数据面（Data Plane）**：音频采样点数组的捕获、清洗（降噪/高通/降采样）、特征提取及模型推理，完全在后台线程（如 I/O 线程、Preprocessor Worker、Inference Worker）中通过**无锁队列**自主闭环流动。
* **控制面（Control Plane）**：`state_manager` **绝不触碰任何音频数据指针，也不执行具体波形运算**。它仅在收到 API 状态切换请求时，通过操作三大生命周期门面（Lifecycle Facades）来间接指挥全链路：

```
                             ┌─────────────────┐
                             │  state_manager  │
                             └────────┬────────┘
                                      │
           ┌──────────────────────────┼──────────────────────────┐
           ▼                          ▼                          ▼
┌────────────────────┐     ┌────────────────────┐     ┌────────────────────┐
│ Model Facade       │     │ IO Facade          │     │ Worker Facade      │
│ 模型加载/卸载      │     │ 设备捕获/播放流    │     │ 后台工作线程启动   │
│ pipeline->switch   │     │ mozart_io_create   │     │ worker->start/stop │
└────────────────────┘     └────────────────────┘     └────────────────────┘
```

### 模式切换 (RT_RVC) 编排控制链流程：
1. **模型大类判定**：若检测到大类变更（如从 RVC 切换为 Zero-Shot VC），控制面首先调用 `Model Facade` 卸载旧大类显存，并执行显式垃圾回收（Python `gc.collect()` 或 C++ `cudaDeviceReset`），之后载入新模型，规避 8GB 共享内存 OOM。
2. **I/O 链路重构**：调用 `IO Facade`（`mozart_io_open_stream`），分配合适大小的无锁环形队列，并激活或断开对应的物理/网络通道。
3. **工作线程激活**：调用 `Worker Facade`（`AudioWorker::start()`）拉起消费线程，数据管道开始流动。

---

## 2. 层间数据传输契约

根据部署模式，层级间的数据传输载体可自适应切换：

### 2.1 同进程模式：C-ABI 契约 + 无锁单写单读环 (SPSC RingBuffer)
在单机物理变声部署中，为了避免锁竞争引发的音频爆音（XRun），预处理和推理后端通过三个无锁单写单读环串联传输。
数据结构采用 `frame_meta.h` 唯一定义的 C-ABI 结构体，强制按 1 字节对齐（`#pragma pack(push, 1)`）：

* `Capture IO` $\xrightarrow{\text{mozart\_raw\_frame\_t}}$ `Preprocessor (C11)`
  * 48kHz / 20ms / 960 浮点点数 + 16B Meta = **3856 字节**。
* `Preprocessor (C11)` $\xrightarrow{\text{mozart\_input\_frame\_t}}$ `Inference Backend`
  * 16kHz / 20ms / 320 浮点点数 + 16B Meta = **1296 字节**（降采样契约帧）。
* `Inference Backend` $\xrightarrow{\text{mozart\_output\_frame\_t}}$ `Playback IO`
  * 48kHz / 20ms / 960 浮点点数 + 16B Meta = **3856 字节**。

### 2.2 跨网络模式：定长 UDP 契约包 (MZRT 协议)
分布式网络模式下，输入客户端与推理后端使用定长 UDP 包交换数据（端口 `18000`）：
* **魔数验证**：包头前 4 字节为固定魔数 `0x4D5A5254`（ASCII 'MZRT'），其后为 16 字节 Meta。
* **输入包**：20B 头部 + 1280B (320点 PCM) = **1300 字节**，严格低于网络 MTU 1500，杜绝分片丢包。
* **输出包**：20B 头部 + 3840B (960点 PCM) = **3860 字节**。

---

## 3. 强互斥状态转移与优雅等待队列

系统定义了 4 种具体模式：
* `RT_RVC`：实时 RVC 变声（独占 PipeWire 音频物理采集与播放，加载 HuBERT+RMVPE+Generator）。
* `FILE_RVC`：离线文件批量变声（断开 PipeWire 设备，仅加载 RVC 核心进行高速批处理）。
* `RT_ZERO_SHOT`：实时零样本变声（加载 Zero-Shot Torch/ONNX 提示词变声核心）。
* `FILE_ZERO_SHOT`：离线零样本变声。

### 双轨控制逻辑
| 当前激活状态 | 目标状态 | 切换逻辑 | 行为描述 |
|---|---|---|---|
| **实时模式 (`RT_*`)** | 任意其他状态 | **立即切换** | 立即中断实时音频流，调用 `IO Facade` 断开 PipeWire 物理绑定以保护隐私，执行目标状态置换。 |
| **非实时模式 (`FILE_*`)** (任务忙碌中) | 任意其他状态 | **优雅等待 (Deferred)** | 切换请求写入全局 Pending 槽。文件转换线程继续运行至 EOF 结束并释放写锁后，在结束回调中自动执行目标模式初始化。 |
| **非实时模式 (`FILE_*`)** (空闲) | 任意其他状态 | **立即切换** | 此时无文件转换线程运行，立刻执行目标模式置换。 |

---

## 4. 双通道算力调度与优先级设计

即便在强互斥状态下，为了彻底杜绝实时音频轨道发生卡顿，系统引入了算力隔离：
1. **CPU 线程调度**：实时音频捕获与输出线程配置为 Linux 实时调度策略 `SCHED_FIFO` (Priority=90)，预处理与推理核心运行在普通优先级，离线文件转换线程强制置为 `nice 19` 极低优先级。
2. **GPU 推理调度 (CUDA Streams)**：
   * 实时变声推理内核提交到高优先级 CUDA 流：`cudaStreamCreateWithPriority(&stream, 0, -1)`。
   * 离线批量转换内核提交到低优先级 CUDA 流：`cudaStreamCreateWithPriority(&stream, 0, 0)`。
   * 这保证了即使大音频文件进行后台批量转换，GPU 也会在硬件级强制抢占（Preempt）执行实时音频样本的变声推理。

---

## 5. 文件缓存与阈值逐退机制 (Post-job Eviction)

`state_manager` 监督本地扁平缓存目录 `storage/temp/`：
* **重命名规则**：所有临时文件均使用微秒级 Unix 时间戳命名（如 `[timestamp]_input.mp3` 及 `[timestamp]_output.wav`），以无用户系统形态规避名字冲突，并实现自然的时间先后排序。
* **逐退算法**：
  每次完成新文件写入时，异步触发容量审计。
  * **高水位**：`MAX_CACHE_SIZE_MB = 1000` (1GB)。若目录总容量超过此线，获取文件列表并按文件名升序排列（即旧到新）。
  * **低水位目标**：`800MB` (高水位的 80%)。从文件头部依次物理删除最旧的文件对，直至总容量降到低水位线以下，释放清理锁。
