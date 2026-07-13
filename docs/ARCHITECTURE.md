# Project Mozart · 系统架构

> 实时 AI 变声器，面向 NVIDIA Jetson Orin Nano 8GB

---

## 1. 两级解耦架构

```
真实麦克风 ──► [ Mozart 预处理 ] ──── 契约流 ────► [ RVC 后处理 ] ──► 扬声器/虚拟麦克风
                  preprocessor/ (C11)        16k/f32/20ms      rvc-backend/ (C++17)
                                     + 16B 元数据
```

| 子系统 | 目录 | 语言 | 职责 |
|--------|------|------|------|
| 预处理 | `preprocessor/` | C11 | 音频清洗：HPF、RNNoise 去噪、自适应混合、3:1 降采样 → 标准契约流 |
| 后处理 | `rvc-backend/` | C++17 | 接收契约流，RVC 变声推理，模型热切换，HTTP 管理 |

**关键原则**：预处理产出"干净的标准流"，后处理只在其上做变声，不再做任何清洗。

---

## 2. 契约（预处理 → 后处理）

### 2.1 音频格式

| 项 | 输入（预处理→后处理） | 输出（后处理→客户端） |
|---|----------------------|---------------------|
| 采样率 | **16 kHz** | **48 kHz** |
| 声道 | mono | mono |
| 格式 | float32 PCM [-1, 1] | float32 PCM [-1, 1] |
| 帧长 | 20 ms（320 样本） | 20 ms（960 样本） |

### 2.2 元数据（16 字节 FrameMeta）

```c
struct __attribute__((packed)) mozart_frame_meta {
    uint64_t pts_ns;       // 纳秒时间戳
    uint32_t frame_idx;    // 帧序号
    uint8_t  vad_flag;     // 0=静音 1=语音
    uint8_t  energy_db;    // 能量 dB (0-255)
    uint8_t  conf;         // 去噪置信度 (0-255)
    uint8_t  segment_id;   // 语音段编号 (0=静音)
};
```

### 2.3 UDP 包格式

```
┌─────────┬──────────┬───────────┬──────┬─────────┬──────┬───────────┬──────────────┐
│ magic   │ pts_ns   │ frame_idx │ vad  │ energy  │ conf │ segment   │ samples[]    │
│ 4B u32  │ 8B u64   │ 4B u32    │ 1B   │ 1B      │ 1B   │ 1B        │ float32[]    │
├─────────┴──────────┴───────────┴──────┴─────────┴──────┴───────────┴──────────────┤
│ 20B header                                                                         │
└────────────────────────────────────────────────────────────────────────────────────┘
```

- 魔数：`0x4D5A5254`（`'MZRT'`）
- 输入帧（16kHz/20ms）：20B + 320×4B = **1300 bytes**
- 输出帧（48kHz/20ms）：20B + 960×4B = **3860 bytes**
- 传输：UDP 端口 **18000**
- VAD bypass：`vad_flag == 0` 时后端跳过推理，节省 GPU

### 2.4 `energy_db` 映射

```c
uint8_t encode_energy_db(float raw_db) {
    float clamped = fmaxf(-96.0f, fminf(0.0f, raw_db));
    return (uint8_t)(clamped + 96.0f);
}
```

详见 [`docs/RVC_BACKEND.md`](RVC_BACKEND.md) 包格式章节。

---

## 3. 项目结构

```
Mozart/
├── README.md                        # 本文档（入口）
├── docs/                            # 📚 全部文档
│   ├── ARCHITECTURE.md              #   系统架构（本文）
│   ├── PREPROCESSING.md             #   预处理开发指南
│   ├── RVC_BACKEND.md               #   RVC 后端开发指南 + 契约
│   ├── FPGA_ROADMAP.md              #   FPGA 远期规划
│   └── HARDWARE_REFERENCE.md        #   ZYNQ 硬件参考
├── preprocessor/                    # 预处理 (C11)
│   ├── mozart.h                     #   公共 C ABI
│   ├── src/                         #   pre.c, rnnoise.c, pipeline.c, ...
│   ├── include/mozart/              #   stage.h, pipeline.h, rnnoise.h
│   ├── native/rnnoise/              #   xiph.org RNNoise v0.2 源码
│   ├── Makefile / CMakeLists.txt
│   └── README.md                    #   简要构建说明
├── rvc-backend/                     # RVC 后端 (C++17)
│   ├── src/                         #   main.cpp, udp_server.cpp, ...
│   ├── include/                     #   network/, rvc/, api/, utils/
│   ├── tests/                       #   test_packet.cpp, test_udp_loopback.cpp
│   ├── config.yaml
│   └── README.md                    #   简要构建说明
└── reference/                       # ZYNQ 硬件参考原理图（原始文件）
```

---



---

## 5. 当前阶段：Phase 1

- [x] 预处理（`preprocessor/`）：RNNoise 去噪、HPF、自适应混合、3:1 降采样
- [x] 后端骨架：UDP 三线程服务器、MZRT 包协议、Mock 直通管线
- [x] C ABI：`mozart_pre_init/process/free` 接口就绪
- [x] HTTP API：`/health`、`/status`、`/models`、`/models/{id}/activate`
- [ ] 真实 RVC Generator 接入（libtorch/ONNX）
- [ ] PipeWire 采集/虚拟源真实实现
- [ ] AEC 集成
- [ ] 延迟优化、`.index` 检索回归
