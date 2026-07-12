# RVC Backend · Interface Contract

> 本文档定义 **Project Mozart 预处理系统** 与 **RVC 变声后端** 之间的接口契约。

---

## 1. 职责边界

| 系统 | 职责 | 不职责 |
|------|------|--------|
| **Project Mozart** | 预处理：去噪、AEC、AGC、瞬态抑制、轻量去混响、VAD/打标 | RVC 推理、模型管理 |
| **RVC Backend** | RVC 变声推理、模型热切换、HTTP 管理 | 任何预处理 |

**原则**：预处理系统产出"干净的标准流"，后端只在其上做变声，不再清洗。

---

## 2. 传输方式

### 2.1 推荐：UDP 网络（跨设备/边缘部署）

适用于预处理在 PC/手机、后端在 Jetson 的边缘部署场景：
- **协议**：UDP，单播
- **端口**：默认 `18000`
- **方向**：双向（预处理 → 后端：16kHz 输入；后端 → 预处理：48kHz 输出）
- **特点**：无连接、低延迟、不保证送达（由应用层通过帧序号处理丢包）

### 2.2 可选：Shared Memory / PipeWire（同机部署）

如果 Mozart 与后端部署在同一台 Jetson 上：
- 可通过 POSIX shared memory 或 PipeWire 虚流直接传递 `FrameBuf`
- 此时不需要 UDP 封装，直接共享内存队列
- 本后端暂只实现 UDP 方案；shared memory 对接可后续扩展

---

## 3. 包格式（UDP）

### 3.1 二进制布局（小端序，C 结构兼容）

```c
struct ContractAudioPacket {
    // --- Header: 20 bytes ---
    uint32_t magic;      // 0x4D5A5254 ('MZRT')
    uint64_t pts_ns;     // 呈现时间戳，纳秒
    uint32_t frame_idx;  // 单调递增帧序号
    uint8_t  vad_flag;   // 1 = 含语音，0 = 静音
    uint8_t  energy_db;  // 能量 dB，映射到 0-255
    uint8_t  conf;       // 预处理器置信度 0-255
    uint8_t  segment_id; // 语音段编号（0 = 静音/未分段）
    
    // --- Payload: float32 PCM ---
    float    samples[];  // 单声道 float32 样本
};
```

### 3.2 各采样率下的包大小

| 采样率 | 帧长 | 样本数 | Payload | 包头 | 总大小 |
|--------|------|--------|---------|------|--------|
| **16 kHz**（输入） | 20 ms | 320 | 1280 B | 20 B | **1300 B** |
| **48 kHz**（输出） | 20 ms | 960 | 3840 B | 20 B | **3860 B** |

> 48 kHz 输出包 3860 字节超过典型以太网 MTU（1500）。在局域网或 Jetson 本地回环中通常无问题；跨互联网建议拆分为多个 10 ms 子帧或启用路径 MTU 发现。

### 3.3 元数据字段详解

| 字段 | 类型 | 说明 | 后端使用方式 |
|------|------|------|-------------|
| `magic` | u32 | `0x4D5A5254` | 包合法性校验 |
| `pts_ns` | u64 | 纳秒时间戳 | 透传，用于客户端音视频同步 |
| `frame_idx` | u32 | 帧序号 | 检测丢包/乱序；透传 |
| `vad_flag` | u8 | 0/1 | **关键**：0 时后端可跳过 RVC 推理，直接输出静音，节省 GPU |
| `energy_db` | u8 | 0-255 | 透传；映射公式：`stored = clamp(raw_dB + 96, 0, 255)` |
| `conf` | u8 | 0-255 | 预处理器置信度；透传 |
| `segment_id` | u8 | 0-255 | 语音段编号；0 = 静音帧；透传 |

---

## 4. 音频格式

### 4.1 输入（Mozart → Backend）
- **采样率**：16 kHz（标准契约）
- **声道**：mono
- **位深**：float32 PCM，范围 [-1.0, 1.0]
- **帧时长**：20 ms（320 样本）
- **因果性**：每帧独立，无跨帧状态（后端按帧处理）

### 4.2 输出（Backend → Mozart/Player）
- **采样率**：48 kHz（RVC v2 生成器输出）
- **声道**：mono
- **位深**：float32 PCM，范围 [-1.0, 1.0]
- **帧时长**：20 ms（960 样本）
- **元数据**：与输入帧一一对应，全部字段透传（`frame_idx`、`pts_ns` 等保持不变）

### 4.3 采样率转换说明

RVC 内部流程：
1. **输入 16 kHz** → 直接用于 HuBERT 特征提取（无需降采样）
2. **F0 提取** @ 16 kHz（RMVPE / harvest / pm）
3. **生成器** 输出 48 kHz 音频

如果未来 Mozart 支持 **48 kHz 变体契约**：
- 后端配置 `input.contract.sample_rate: 48000`
- 后端内部先做 48kHz → 16kHz 降采样，再进行特征提取
- 生成器仍输出 48kHz

---

## 5. 行为契约

### 5.1 静音帧处理（VAD Bypass）

当 `vad_flag == 0` 时：
- **后端默认行为**：跳过 RVC 推理，直接返回对应长度的静音帧（`samples = 0.0`）
- **目的**：节省 GPU 计算，降低功耗和延迟
- **配置**：可通过 `config.yaml` 中 `input.meta.vad_enabled` 开关

### 5.2 丢包与乱序

- **丢包**：后端不补偿。输出帧序号严格对应输入 `frame_idx`，缺失的帧不产生输出。
- **乱序**：后端应按 `frame_idx` 排序后再送入 RVC 推理；但实时场景下建议前端保证顺序。

### 5.3 帧积累与批量推理

- **默认**：每帧独立推理（`frames_per_inference: 1`），最低延迟
- **批量模式**：可配置 `frames_per_inference: N`，积累 N 帧后批量送入 GPU，提高吞吐但增加 N×20ms 延迟

---

## 6. C 结构参考（供 Mozart 客户端实现）

```c
#pragma pack(push, 1)

#define CONTRACT_MAGIC 0x4D5A5254

struct ContractHeader {
    uint32_t magic;
    uint64_t pts_ns;
    uint32_t frame_idx;
    uint8_t  vad_flag;
    uint8_t  energy_db;
    uint8_t  conf;
    uint8_t  segment_id;
};

// 16 kHz / 20 ms
#define INPUT_SAMPLES_PER_FRAME 320
struct InputPacket {
    struct ContractHeader hdr;
    float samples[INPUT_SAMPLES_PER_FRAME];
};

// 48 kHz / 20 ms
#define OUTPUT_SAMPLES_PER_FRAME 960
struct OutputPacket {
    struct ContractHeader hdr;
    float samples[OUTPUT_SAMPLES_PER_FRAME];
};

#pragma pack(pop)
```

---

## 7. 配置对接

后端 `config.yaml` 中影响契约的关键项：

```yaml
input:
  contract:
    sample_rate: 16000        # 必须与 Mozart 输出一致
    frame_duration_ms: 20     # 必须与 Mozart 帧长一致
  meta:
    enabled: true             # 启用元数据解析
    vad_enabled: true         # 启用静音 bypass

output:
  sample_rate: 48000          # RVC 生成器输出，固定 48kHz

network:
  audio:
    port: 18000               # Mozart 发送目标端口
    frames_per_inference: 1   # 1 = 最低延迟；>1 = 批量推理
```

---

## 8. 变更记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v0.1 | 2025-07-11 | 初始版本，定义 Mozart → RVC Backend 契约流格式 |
