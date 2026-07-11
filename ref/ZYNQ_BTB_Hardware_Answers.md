# ZYNQ BTB 核心板 + 底板硬件问答

> 基于原理图 PDF 分析，日期：2026-07-11

---

## 1. 核心板 FPGA 型号

两份器件值 PDF 均存在，PCB 版本兼容两种芯片：

| 器件 | PDF 文件 |
|------|---------|
| XC7Z010-1CLG400I | `EBF410020_器件值_20230724_ZYNQ7010.pdf` |
| XC7Z020-2CLG400I | `EBF410020_器件值_20230724_ZYNQ7020.pdf` |

**结论**：同一版 PCB 可焊 7010 或 7020，需确认实际板子焊接的是哪颗（读 ZYNQ IDCODE 或检查丝印）。

二次更新：我买的7020的

---

## 2. BTB 底板音频 I²S 引脚

底板有 6 路 I²S 音频信号，全部走 PL 侧 **Bank 34**，通过 BTB 连接器引出：

| 信号名 | ZYNQ PL 引脚 | Package Pin | Bank |
|--------|-------------|-------------|------|
| AUDIO_ADCLRC | IO34_L4P | V12 | Bank 34 |
| AUDIO_DACLRC | IO34_L4N | W13 | Bank 34 |
| AUDIO_DACDAT | IO34_L10P | V15 | Bank 34 |
| AUDIO_MCLK | IO34_L10N | W15 | Bank 34 |
| AUDIO_BCLK | IO34_0 | R19 | Bank 34 |
| AUDIO_ADCDAT | IO34_L13N | P19 | Bank 34 |

- 全部在 **PL Bank 34**（HR Bank，3.3V I/O），不是 PS MIO。
- 控制接口走 I2C1_SCL / I2C1_SDA（挂 PS I²C1）。

---

## 3. 板载音频 Codec

**有板载音频 Codec**，型号为：

### **WM8960CGEFL**（Wolfson / Cirrus Logic）

关键特性：
- I²S 数字音频接口（ADCLRC / DACLRC / BCLK / ADCDAT / DACDAT / MCLK）
- 内置立体声 ADC + DAC
- 内置耳机放大器（HP_L / HP_R 输出，可直驱耳机）
- 内置 Class-D 扬声器放大器（SPK_LP / SPK_LN / SPK_RP / SPK_RN，BTL 方式驱动 2×8Ω 1W 喇叭）
- 控制接口：I²C（地址由 CSB 引脚决定）
- 供电：DCVDD / DBVDD / SPKVDD1 / SPKVDD2 / AVDD，典型 3.3V

**不是** SSM2603 / WM8730 / CS42L52，确认是 **WM8960**。

---

## 4. MEMS 麦克风 / 外接方案

### 板上没有焊接 MEMS 麦克风，需要外接。

外接接口：

| 接口 | 连接器型号 | 用途 |
|------|-----------|------|
| J12 | PJ-325C5-R（3.5mm） | 外接麦克风，带 MICBIAS1 偏置（可驱动驻极体麦） |
| J13 | PJ-325C5-G（3.5mm） | 耳机/耳麦插座，支持带麦耳麦（MIC1P/MIC2P + HP_JD 插拔检测） |
| J14 | PH-4A（4pin 排针） | 扬声器输出，8Ω 1W BTL |

总结：**3.5mm 音频插座**（J12/J13），不是排针。耳机插入检测信号 HP_JD 为低电平有效。

---

## 5. ZYNQ PS 端 UART 引脚

### 是，UART 引脚通过 BTB 暴露在底板上。

底板提供 **三种串口接口**：

| 接口 | 芯片/连接器 | 电平 | 说明 |
|------|-----------|------|------|
| USB 转串口 | CH340C + 121U31 + Type-C（J21） | USB | 即插即用，连接 PC |
| TTL 排针 | HDR2X2（J22，2.54mm） | 3.3V TTL | UART_TX / UART_RX 直出 |
| RS232 | MAX3232CSE + DB9 母头（J7） | RS232 | 标准串口 |

- UART 信号来源：**PS UART1**，对应 MIO14（UART1_TX）和 MIO15（UART1_RX），Bank 500。
- 通过 BTB 连接器传至底板，再经电平转换/接口芯片对外暴露。

---

## 6.底板与核心板的连接 

### 使用 BTB (Board-to-Board) 连接器，夹层直插结构。

| 位置 | 连接器型号 | 引脚数 |
|------|-----------|--------|
| 核心板侧（公座） | SGDBF-05-60P-H35 | 60pin × 4 = 240pin |
| 底板侧（母座） | SGDBM-05-60P-H10 | 60pin |

- **0.5mm 间距、60pin 双排高密度 BTB 连接器**
- 共 4 组连接器，总计 240pin
- 底板上还有额外 BTB 扩展座（SGDBF-05-60P-H30 / SGDBM-05-60P-H10），可用于级联

**不是** 40-pin 排针直插，**不是** HDMI 扩展口，是 **BTB 对 BTB 夹层结构**（Sandwich 模式）。

---

## 快速参考表

| 项目 | 答案 |
|------|------|
| FPGA 型号 | XC7Z010 或 XC7Z020（需确认实际焊接） |
| I²S 引脚 | 6 路，全在 PL Bank 34（V12 / W13 / V15 / W15 / R19 / P19） |
| 音频 Codec | **WM8960CGEFL**（I²S + Class-D 功放） |
| 板载 MEMS 麦克风 | **无**，外接 3.5mm（J12/J13） |
| UART 暴露 | 是，三种：USB（CH340C）、TTL 排针（J22）、RS232（J7） |
| 底板与核心板连接 | **BTB 240pin**（SGDBF/SGDBM-05-60P），夹层直插 |
