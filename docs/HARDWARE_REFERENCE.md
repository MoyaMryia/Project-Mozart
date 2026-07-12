# ZYNQ BTB 核心板 + 底板硬件参考

> 基于 `reference/` 目录中的原理图 PDF 分析  
> 适用于 FPGA 子卡（Phase 2）选型参考

---

## 1. 核心板 FPGA 型号

同一 PCB 可焊两种芯片：

| 器件 | PDF 文件 |
|------|---------|
| XC7Z010-1CLG400I | `EBF410020_器件值_20230724_ZYNQ7010.pdf` |
| XC7Z020-2CLG400I | `EBF410020_器件值_20230724_ZYNQ7020.pdf` |

当前实际为 **XC7Z020-2CLG400I**。

---

## 2. BTB 底板音频 I²S 引脚

6 路 I²S 音频信号，全部在 **PL Bank 34**（HR Bank, 3.3V I/O）：

| 信号名 | ZYNQ PL 引脚 | Package Pin |
|--------|-------------|-------------|
| AUDIO_ADCLRC | IO34_L4P | V12 |
| AUDIO_DACLRC | IO34_L4N | W13 |
| AUDIO_DACDAT | IO34_L10P | V15 |
| AUDIO_MCLK | IO34_L10N | W15 |
| AUDIO_BCLK | IO34_0 | R19 |
| AUDIO_ADCDAT | IO34_L13N | P19 |

控制接口走 I2C1_SCL / I2C1_SDA（PS I²C1）。

---

## 3. 板载音频 Codec

**WM8960CGEFL**（Wolfson / Cirrus Logic）

- I²S 数字音频接口
- 内置立体声 ADC + DAC
- 内置耳机放大器（HP_L / HP_R，可直驱耳机）
- 内置 Class-D 扬声器放大器（SPK_LP/LN/RP/RN，BTL 驱动 2×8Ω 1W 喇叭）
- 控制接口：I²C
- 供电：DCVDD / DBVDD / SPKVDD1/2 / AVDD，典型 3.3V

---

## 4. 外接麦克风

板上**没有**焊接 MEMS 麦克风，需外接：

| 接口 | 连接器 | 用途 |
|------|--------|------|
| J12 | PJ-325C5-R（3.5mm） | 外接麦克风（带 MICBIAS1 偏置） |
| J13 | PJ-325C5-G（3.5mm） | 耳机/耳麦插座（支持 MIC1P/MIC2P + HP_JD） |
| J14 | PH-4A（4pin 排针） | 扬声器输出，8Ω 1W BTL |

---

## 5. ZYNQ PS 端 UART

| 接口 | 芯片/连接器 | 电平 |
|------|-----------|------|
| USB 转串口 | CH340C + 121U31 + Type-C（J21） | USB |
| TTL 排针 | HDR2X2（J22, 2.54mm） | 3.3V TTL |
| RS232 | MAX3232CSE + DB9 母头（J7） | RS232 |

UART 信号来源：**PS UART1**（MIO14 TX, MIO15 RX, Bank 500）。

---

## 6. 核心板↔底板连接

BTB (Board-to-Board) 连接器，夹层直插结构：

| 位置 | 连接器 | 引脚数 |
|------|--------|--------|
| 核心板侧（公座） | SGDBF-05-60P-H35 | 60pin × 4 = 240pin |
| 底板侧（母座） | SGDBM-05-60P-H10 | 60pin |

0.5mm 间距、60pin 双排高密度 BTB 连接器，共 4 组，总计 240pin。

---

## 快速参考表

| 项目 | 答案 |
|------|------|
| FPGA 型号 | XC7Z020-2CLG400I |
| I²S 引脚 | 6 路，全在 PL Bank 34（V12/W13/V15/W15/R19/P19） |
| 音频 Codec | **WM8960CGEFL**（I²S + Class-D 功放） |
| 板载 MEMS 麦克风 | **无**，外接 3.5mm（J12/J13） |
| UART 暴露 | 是，三种：USB（CH340C）、TTL 排针（J22）、RS232（J7） |
| 底板连接 | **BTB 240pin**（SGDBF/SGDBM-05-60P），夹层直插 |

> 完整原理图文件位于 `reference/EBF_ZYNQ70xx_BTB_CORE/` 和 `reference/EBF_ZYNQ70xx_BTB_Plate/`。
