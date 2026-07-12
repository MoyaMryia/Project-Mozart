# Mozart 预处理 · 开发指南

> 纯 C11 音频预处理管线，面向 NVIDIA Jetson Orin Nano
> ARM NEON + DOTPROD + FP16 优化 · RNNoise 实时降噪

---

## 1. 概述

预处理是 AI 声卡的前半段，负责把原始麦克风音频处理成标准契约流。

```
  Raw Mic ──► [ 80Hz HPF ] ──► [ RNNoise ] ──► [ Adaptive Mix ] ──► [ 3:1 Decimate ] ──► Contract Output
              (IIR biquad)      (GRU denoiser)   (SNR-based wet/dry)   (480→160×2)        16kHz/f32 + 12B meta
```

**设计决策：**
- 纯 **C11**，无运行时、无动态分配
- **ARM NEON** 向量路径（Cortex-A78AE）
- **函数指针 vtable** 架构：每个处理单元是可插拔的 stage
- 12 字节 packed 元数据与 C++ 后端保持 ABI 一致

---

## 2. 目录结构

```
preprocessor/
├── mozart.h                     ← 公共 C ABI（消费者只需此头文件）
├── Makefile                     ← 主构建（ARM 优化）
├── CMakeLists.txt               ← CMake 备选构建
├── include/mozart/
│   ├── stage.h                  ← Stage vtable 接口
│   ├── pipeline.h               ← 有序 Stage 链
│   ├── rnnoise.h                ← RNNoise 构造器
│   └── pipewire.h               ← PipeWire 采集/虚拟源
├── src/
│   ├── pre.c                    ← 顶层入口：HPF → RNNoise → mix → decimate
│   ├── rnnoise.c                ← RNNoise 封装（float32↔int16、VAD）
│   ├── pipeline.c               ← Stage 链顺序处理
│   ├── stage.c                  ← 通用 Stage 生命周期
│   ├── pipewire.c               ← PipeWire stub
│   └── main.c                   ← 冒烟测试
├── native/rnnoise/              ← xiph.org RNNoise v0.2 源码子树
│   ├── include/rnnoise.h
│   └── src/                     ← denoise, rnn, nnet, kiss_fft, pitch, …
├── tests/
│   └── test_stage.c             ← Stage 单元测试
└── build/                       ← 构建产物
```

---

## 3. 核心类型

### 3.1 `mozart_frame_meta_t` — 12 字节帧元数据

```c
typedef struct __attribute__((packed)) {
    uint64_t pts_ns;       // 呈现时间戳 (ns)
    uint32_t frame_idx;    // 帧序号
    uint8_t  vad_flag;     // 0=静音, 1=语音
    uint8_t  energy_db;    // 能量 dB (0-255)
    uint8_t  conf;         // 去噪置信度 (0-255)
    uint8_t  segment_id;   // 语音段 ID (0=间隙)
} mozart_frame_meta_t;
```

此结构与 `rvc-backend/include/network/packet.hpp` 中的 `FrameMeta` 保持字节一致。

### 3.2 `mozart_stage_t` — 可插拔处理单元

```c
struct mozart_stage {
    mozart_stage_vtable_t vtable;   // { .name, .process, .destroy }
    void                 *data;     // 私有状态
};
```

### 3.3 `mozart_pipeline_t` — 有序 Stage 链

最多 **8 个 stages**，按插入顺序执行，前一级输出自动喂给下一级。

### 3.4 `mozart_pre_ctx_t` — 不透明顶层句柄

`mozart_pre_init()` 创建，`mozart_pre_free()` 销毁。

---

## 4. 处理流程（`src/pre.c`）

```
48kHz 帧 (960 float32)
    │
    ▼
80Hz 高通滤波（4阶 Butterworth，双二阶级联）
    │
    ▼
拆分为 2 个 10ms 子帧 (480 样本/个)
    │
    ▼
对每个子帧：
    ├── float32 → int16 转换
    ├── RNNoise 去噪 (rnnoise_process_frame)
    ├── int16 → float32 转换
    ├── 从 DenoiseState→vad_prob 提取 VAD 置信度
    └── 自适应湿干混合（高 SNR → 更多干信号）
    │
    ▼
3:1 线性降采样（480 样本 → 160 样本/子帧）
    │
    ▼
输出：16kHz 帧 (320 float32) + 12B FrameMeta
```

---

## 5. 公共 C ABI（`mozart.h`）

### 5.1 常量

```c
#define MOZART_CONTRACT_SAMPLE_RATE    16000
#define MOZART_CONTRACT_FRAME_MS       20
#define MOZART_CONTRACT_SAMPLES        320
#define MOZART_CONTRACT_META_SIZE      12
#define MOZART_INTERNAL_SAMPLE_RATE    48000
#define MOZART_INTERNAL_FRAME_MS       10
#define MOZART_INTERNAL_SAMPLES        480
```

### 5.2 生命周期

| 函数 | 描述 |
|------|------|
| `mozart_pre_init(cfg)` | 创建管线，连接 stages，返回不透明 ctx |
| `mozart_pre_process(ctx, in, n, out, meta)` | 推送一帧音频通过所有 stages |
| `mozart_pre_free(ctx)` | 销毁管线，释放资源 |
| `mozart_pre_version()` | 返回编译期版本字符串 |

### 5.3 Stage 构造器

| 构造器 | 模块 | 状态 |
|--------|------|------|
| `mozart_rnnoise_new()` | RNNoise 去噪 | Ready |
| `mozart_pw_capture_new()` | PipeWire 采集 | Stub |
| `mozart_pw_source_new()` | 虚拟源 | Stub |

### 5.4 实现新 Stage

```c
mozart_stage_vtable_t vt = {
    .name    = "my_module",
    .process = my_process_callback,
    .destroy = my_destroy_callback,
};
mozart_stage_t *s = mozart_stage_new(&vt, my_private_data);
// 然后加入 pipeline：
mozart_pipeline_add_stage(ctx->pipeline, s);
```

---

## 6. RNNoise 集成

### 构建模式

| 模式 | `MOZART_USE_RNNOISE` | 行为 |
|------|----------------------|------|
| 真实去噪 | `1`（默认） | 链接 `native/rnnoise/`，GRU 神经网络降噪 |
| 直通 stub | 未定义 | 输入直接拷贝到输出，不做处理 |

### ARM NEON 验证

通过在 Makefile 中指定 `-march=armv8.2-a+dotprod+fp16 -mtune=cortex-a78ae` 启用。`native/rnnoise/src/vec.h` 中的 dispatch 会在 ARM 上自动选择 `vec_neon.h`。

---

## 7. 构建

```bash
make          # Release（含 RNNoise，ARM NEON 优化）
make debug    # Debug (-g -O0)
make stub     # 直通（不依赖 RNNoise）
make run      # 构建并运行冒烟测试
make clean    # 清理
```

CMake 备选：

```bash
cmake -S . -B build -DMOZART_USE_RNNOISE=ON
cmake --build build
```

---

## 8. 管线 Stages 蓝图

| # | 模块 | 状态 | 文件 |
|---|------|------|------|
| ① | PipeWire I/O | Stub | `src/pipewire.c` |
| ② | 归一化 | 未开始 | — |
| ③ | 瞬态抑制 | 未开始 | — |
| ④ | AEC | 未开始 | — |
| ⑤ | **RNNoise 去噪** | **Ready** | `src/rnnoise.c` |
| ⑥ | 轻量去混响 | 未开始 | — |
| ⑦ | AGC + 削波 | 未开始 | — |
| ⑧ | VAD/打标 | 未开始 | — |

当前 `src/pre.c` 已将 ⑤ + 部分 ⑧（RNNoise 内置 VAD）+ HPF + 混合 + 降采样集成为一个优化路径。

---

## 9. 平台：Jetson Orin Nano

| 项 | 值 |
|----|-----|
| CPU | 6 × Cortex-A78AE @ 1.728 GHz |
| ISA | ARMv8.2-A + NEON + FP16 + DOTPROD |
| 内存 | 7.4 GiB LPDDR5 |
| GPU | Ampere SM 8.7（预留给后处理） |
| 音频后端 | PipeWire 1.0.5 |
| 编译器 | GCC 13.3 |
| OS | Linux 6.8.12-tegra, PREEMPT |

---

## 10. 延迟预算

| 阶段 | 预算 |
|------|------|
| 80Hz HPF | <0.05 ms |
| RNNoise 去噪 | <0.5 ms |
| 自适应混合 | <0.05 ms |
| 3:1 降采样 | <0.1 ms |
| **算法总计** | **<1 ms** |
