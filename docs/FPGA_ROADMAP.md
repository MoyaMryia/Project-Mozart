# AI 声卡 · FPGA 远期规划

> **当前阶段**：Phase 0 — 纯 C 软件 RNNoise（`preprocessor/` 已实现）  
> **远期路线**：SW RNNoise → ADSP 固件(APE) → FPGA SoC 子卡

---

## 1. 路线图

```
 Phase 0 (当前)          Phase 1              Phase 2
 SW RNNoise(Jetson C) → ADSP 固件(APE) →  FPGA SoC 子卡
 "先可改通跑"          "离硬件最近一跳"       "独立声卡枚举"
```

---

## 2. FPGA 接入拓扑

**推荐：I²S/TDM 流式前端（拓扑 B）**

FPGA 作为 I²S/TDM master，Jetson APE 作为 slave 收 PCM。FPGA 子卡本身就是一张"AI 麦克风声卡"：

```
 ┌─────────────────── FPGA 子卡 ───────────────────┐
 │  [PDM MEMS 麦阵列]                               │
 │       │ PDM → PCM (CIC+FIR)                     │
 │       ▼                                         │
 │  [可选: 波束形成/DOA]                            │
 │       ▼                                         │
 │  [RNNoise kernel (定点硬件实现)]                  │
 │       ▼                                         │
 │  [I²S/TDM OUT] ──master── BCLK/LRCK/DATA        │
 └────────────────────────┬────────────────────────┘
                          │
                          ▼
 ┌── Jetson APE (ADMAIF) ──┐
 │ I²S slave-in            │
 │ → ALSA → PipeWire source│ (已净化 PCM)
 └──────────────────────────┘
```

**选择理由：**
- Jetson 端零代码路由：FPGA 输出出现在 `tegra-ape` 的 ADMAIF 通道上，PipeWire 自动认作 source
- 音频时钟由 FPGA 板载 TCXO 驱动，与软件时钟解耦
- 元数据走独立 UART/SPI 通道，不干扰 I²S 时钟

---

## 3. 与 C 管线的接口

FPGA 接入**不动主管线契约**，通过 `Preprocessing.md` 中描述的 C ABI 配置切换：

```c
// Phase 0: 纯软件 RNNoise（当前）
cfg.mode = MOZART_MODE_SW_RNNOISE;

// Phase 2: FPGA 已做去噪，C 侧退化为 IdentityStage
cfg.mode = MOZART_MODE_FPGA_PASSTHROUGH;
```

Jetson 侧 `mozart_pre_process()` 输出格式不变：16kHz/f32/20ms + 16B meta。

---

## 4. 数据通路参数

| 项 | 值 |
|----|-----|
| 采样率 | 48 kHz |
| 帧大小 | 480 samples（10 ms） |
| 音频通路 | I²S/TDM |
| 位宽 | 24-bit / 16-bit |
| 元数据通路 | 独立 UART/SPI |
| 附加延迟 | ~10 ms（1 帧 RNNoise 窗口） |

---

## 5. FPGA 能力边界

- ✅ RNNoise / 波束形成 / 轻量 AEC / AGC
- ❌ 人声识别类大模型（ASR / 说话人 / 情感 / LLM）— 留 Jetson GPU
  - 理由：XC7Z020 PL BRAM 仅 630 Kb，大模型需要 PS DDR
  - Jetson Ampere GPU + TensorRT 才是大模型的本职载体
- ❌ AEC 自适应步长对定点/截断敏感，留 Jetson CPU（WebRTC）

---

## 6. 参考硬件

当前 `reference/` 目录存有 ZYNQ7020 BTB 核心板原理图：

- **核心板**：EBF ZYNQ7020 BTB Core Board（XC7Z020-2CLG400I）
- **底板**：EBF ZYNQ BTB Base Plate
- **板载 Codec**：WM8960CGEFL（I²S 接口）
- **I²S 引脚**：6 路全部在 PL Bank 34
- **控制接口**：PS I²C1

详见 [`HARDWARE_REFERENCE.md`](HARDWARE_REFERENCE.md)。

---

## 7. 落地节奏

1. 在 Jetson CPU 把整条 C 管线跑通，固化契约 → ✅ Phase 0 已完成
2. 在 APE ADSP 上验证定点量化误差（Phase 1）
3. 将定点模型搬到 FPGA 子卡，Jetson 侧仅换收音源 + 配置项（Phase 2）
