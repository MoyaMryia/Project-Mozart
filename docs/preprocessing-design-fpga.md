# AI 声卡 · 预处理阶段设计文档（FPGA 版）

> Project Mozart · 预处理设计 v0.1（含 FPGA 扩展附录）
> 纯 Jetson 版见 `docs/preprocessing-design.md`

## 1. 设计目标

把"声卡"抽象成两级解耦流水线：

- **预处理**：把脏采集流洗净、归一化、分段标注，产出一个**有契约保证**的标准流。
- **后处理**（后续）：在干净流上挂复杂 AI 能力，无需再做任何清洗。

预处理要做到三件事：

1. **Causal 实时**：所有模块只依赖过去和当前样本，不允许整段回看（混合模式约束）。
2. **速度优先**：在不丢质量的前提下压低 CPU 与延迟，单帧端到端预算 ≤ 5 ms（见 §7）。
3. **可插拔去噪**：去噪是预处理里最重的一块，默认 RNNoise（见 §6），保留 trait 以便后续切换。

非目标（本期不解决）：
- 驱动级/内核态实现（见 §5 部署策略）。
- 后处理任何能力。
- 模型训练。

## 2. 整体架构

```
                  ┌───────────────────────── 预处理（本期）─────────────────────────┐
原始采集流 ──► ① I/O 层      ② 归一化     ③ 瞬态抑制 ④ AEC   ⑤ 去噪   ⑥ 轻量去混响 ⑦ AGC+削波 ⑧ VAD/打标
(PipeWire/     (抓流+时钟   (重采样/位深  (键盘/风扇   (远端参   (Densoiser   (因果DR)   (自动增益    (切段+时间戳
 WASAPI)        对齐)       /声道归一)   检测抑制)    考对消)   trait,可切换)            +直流+限幅)   +能量+可信度)
 loopback)                                                                                       │
                                                                                                 ▼
                                                                                       ┌────────────────┐
                                                                                       │ 契约流封装器     │ 16kHz/mono/f16/20ms
                                                                                       │ FramePacker     │ + 元数据侧带
                                                                                       └────────────────┘
                                                                                                 │
================================================================================================ │  契约边界
                                                                                                 ▼
                                                                                          后处理（后续）
```

混合模式约束落在每个模块上：①~⑧ 全部因果可流式。段落级非因果（如对一句话做双向谱减）留到后处理做。

## 3. 模块职责

| # | 模块 | 输入 | 输出 | 关键约束 |
|---|------|------|------|----------|
| ① | I/O 层 | 系统音频回调(PipeWire/WASAPI) | 固定时长帧 + PTS | 不可阻塞，背压控流 |
| ② | 归一化 | 任意采样率/位深/声道帧 | 48kHz/f32/mono 帧 | 重采样必须因果(speex/rubato) |
| ③ | 瞬态抑制 | mono 帧 | mono 帧 | 检测并衰减键盘/风扇 click |
| ④ | AEC | mic 帧 + 远端参考帧 | 残差帧 | 远端参考来自系统回放环回 |
| ⑤ | 去噪 | 残差帧 | 增强帧 | trait,默认 RNNoise |
| ⑥ | 轻量去混响 | 增强帧 | 干声帧 | 因果版,模型或倒谱法 |
| ⑦ | AGC+削波 | 干声帧 | 增益规整帧 | 目标电平 -23 LUFS,防削波 |
| ⑧ | VAD/打标 | 增益规整帧 | 段边界 + 元数据 | Silero VAD 或 RNNoise 内置 |

各模块签名用 Rust trait 描述（C 侧通过 FFI 调一个 `process_frame`）：

```rust
pub trait Stage {
    fn process(&mut self, in_frame: &[f32], meta_in: &FrameMeta) -> Result<FrameBuf>;
}
```

## 4. 契约（预处理 → 后处理）

这是两级架构的硬接口，先冻结。

### 4.1 音频帧
- 采样率：**16 kHz**（预处理内部 48 kHz 计算；输出降采样到 16 kHz，因 ASR/VAD/说话人等下游模型多为 16 kHz）
- 声道：**mono**
- 位深：**float32 PCM, [-1, 1]**
- 帧时长：**20 ms（320 samples）**

> 内部 48 kHz 而输出 16 kHz 是一个有意决策：去噪/AEC 在 48 kHz 全带上做才能保高频细节，再降采样给下游。若后处理将来需要 48 kHz，契约端加一个变体即可。

### 4.2 元数据侧带
每帧附 12 字节头：

```
[PTS_ns: u64][frame_idx: u32][vad_flag: u8][energy_db: u8][conf: u8][segment_id: u8]
```
- `vad_flag`：本帧是否含语音
- `segment_id`：语音段编号，跨帧连续；静音帧 segment_id=0
- `conf`：去噪置信度（0-255），后处理可据此降权

### 4.3 传输形态
- 进程内：`FrameBuf { samples: Box<[f32;320]>, meta: FrameMeta }` 走 mpmc 无锁队列
- 跨进程/跨语言：每帧 = 4 字节魔数 + 12 字节 meta + 1280 字节 PCM = 1296 字节定长包，走 shared ring 或 PipeWire 虚流

## 5. 部署策略：用户态 + PipeWire 虚设备

驱动级会拖慢迭代且崩内核，所以选用户态。但要让其它应用"看得见"——这是"声卡"相对普通音频增强库的本质价值。

```
       真实麦克风 ─► PipeWire ─► 我们的进程 ─► 预处理 ─► 注册成 virtual source
                                                          ▲
                                                          │ 其它应用(OBS/浏览器/会议软件)
                                                          │ 把 virtual source 当麦克风选
```

实现可选（最终路径已选）：
- **A. 复用现成 ✅（先行）**：直接用 DeepFilterNet 的 LADSPA 插件 + PipeWire filter-chain 配置（见其 `ladspa/README.md`，最小延迟 20ms STFT + host）。零开发快速验证 virtual source 能被 OBS/浏览器/会议软件当麦克风采。
- **B. 自主管线（后置）**：验证链路可行后，再用 `pipewire-rs` 自建 capture→§2 全管线→virtual source，把 LADSPA 替换下来。

落地顺序：先 A 跑通路由 → 再 B 接管管线（改用 RNNoise）。Windows 走 WASAPI loopback 同思路。

## 6. 去噪模型选型

去噪是预处理最重模块。经对比后**确定采用 RNNoise**。

### 6.1 选型理由

- **速度优先**：纯 C、无运行时依赖、单帧 <10 µs 量级，极致满足"运行速度越快越好"。
- **维护良好**：xiph 2024 年发布 v0.2，2025 年更新训练数据和 RIR 数据集。
- **可动态加载权重**：支持运行时从二进制加载 little/标准两种大小模型（`rnnoise_model_from_file()`），或编译期嵌入。
- **FPGA 可移植（关键决策因素）**：RNNoise 是小型 GRU 网络，权重可直接固化为 C 数组、定点化友好、无运行时/外部库依赖，适合未来搬到 FPGA 硬件加速。对比之下 DeepFilterNet 的 STFT + 复值深滤波在硬件实现会显著更复杂。
- **混用契合**：纯 C 库，Rust 侧薄 FFI 即可包成 `Denoiser` trait。

### 6.2 候选对比（留档备查）

| 维度 | RNNoise ✅ | DeepFilterNet | PercepNet |
|---|---|---|---|
| 算法延迟 | <10 ms | 20 ms (STFT) | ~20 ms |
| 单帧 CPU | 极低 | 低 | 低 |
| 质量 | 中 | 中上 | 中上 |
| 语言 | C | Rust+libDF+C ABI | C(移植) |
| FPGA 友好度 | **高** | 中 | 中 |
| License | BSD-3 | MIT/Apache 双 | BSD-like |

> 将来如果质量不足以满足后处理输入要求，再在 trait 后加 DeepFilterNet 档；当前选择 RNNoise。

### 6.3 RNNoise 集成要点

- 输入约束：48 kHz / mono / 16-bit PCM 或 f32（将 48kHz→16kHz 降采样放在去噪之后）。
- 模型加载：默认编译期嵌入 `rnnoise_data.c`，预留 `rnnoise_model_from_file()` 路径以便将来热更/不同档位权重。
- 架构上：`crates/mozart-rnnoise/` 包 C FFI，实现 `mozart-core::Denoiser` trait；`native/rnnoise/` 是 xiph 源码子树，由 CMakeLists.txt 编译静态库供 cargo build.rs 链接。

## 7. 延迟预算（单帧 20ms 端口）

| 阶段 | 预算 | 说明 |
|---|---|---|
| I/O 等待 | 20 ms | 一个端口周期 |
| 归一化 | <0.1 ms | 重采样因果 |
| 瞬态抑制 | <0.1 ms | 检测门 |
| AEC | ~0.2 ms | WebRTC AEC3 |
| 去噪 RNNoise | <0.5 ms | 见 §6 |
| 去混响 | <0.3 ms | 轻量 |
| AGC+VAD | <0.1 ms | |
| **总计** | ≈ 21 ms | 算法延迟≈1ms |

目标：从声卡回调到我们 virtual source 可读，端到端额外延迟 **≤ 5ms**。

## 8. 目录结构（C/C++/Rust 混用）

```
mozart/
├─ crates/
│  ├─ mozart-core/      共享类型(FrameBuf,FrameMeta,Stage trait),Rust
│  ├─ mozart-pre/       预处理主管线(组装各Stage),Rust
│  ├─ mozart-rnnoise/   RNNoise FFI 封装(包 xiph C 库),Rust↔C
│  ├─ mozart-dfnet/     DeepFilterNet libDF FFI 封装,Rust
│  ├─ mozart-webrtc/    WebRTC AEC3 子集封装(C++ → Rust via cxx),Rust↔C++
│  └─ mozart-pw/        PipeWire 抓流+virtual source,pipewire-rs
├─ native/
│  ├─ rnnoise/         (子树/子系统) xiph rnnoise C 源
│  ├─ webrtc-audio/    WebRTC audio_processing 需要的 AEC3 子树,C++
│  └─ deepfilternet/   (子模块) libDF 已有 C ABI,直接链接
├─ mozart.h            给后处理/C 侧的统一 C ABI
├─ CMakeLists.txt      编译 C 侧子树,产物供 cargo build.rs 调用
└─ docs/
   └─ preprocessing-design.md   本文档
```

ABI 形态（`mozart.h` 草案）：

```c
typedef struct mozart_ctx mozart_ctx;
mozart_ctx* mozart_pre_init(const mozart_config* cfg);
int         mozart_pre_process(mozart_ctx*, const float* in, int n,
                               float* out, mozart_frame_meta* meta);
void        mozart_pre_free(mozart_ctx*);
```
这样后处理（无论是 Rust 还是 C/C++）只看到一个 C 头。

## 9. 风险与待决

- [ ] PipeWire 路由：virtual source 在某些发行版默认不被会议软件识别为麦克风，需验证 device profile。
- [ ] AEC 远端参考：从系统回放环回抓远端参考，需选 PipeWire monitor 流方案，跨分发版行为不一。
- [ ] RNNoise 48kHz→16kHz 降采样位置：先去噪再降，还是先降再去噪？倾向先在 48kHz 去、再降采样给契约（保高频滤波余量）。
- [ ] DFNet libDF 二进制协议与许可：libDF 是 Apache/MIT，可静态链接，但仍需在 NOTICE 中声明。
- [ ] 性能基线：优先建立一段脏语音的端到端延迟与 CPU 测试床，用于 AB。

## 10. 下一步

1. ~~去噪选型~~ → 已定 RNNoise。
2. ~~部署路径~~ → 方案 A 先行（复用现成 LADSPA 插件跑通虚拟麦克），再 B 自主管线接管。
3. 契约冻结 → 暂缓，待后续推进后处理时再决定。
4. **下一个动作**：在本机配 DeepFilterNet LADSPA 插件 + PipeWire filter-chain，验证 OBS 浏览器等能选到该 virtual source 当麦克风。

## 附录 A：目标平台画像与优化机会

### A.1 硬件画像

本机是 **NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super**（tegra234），与 x86 桌面机区别极大，需要专门优化才有竞争力。

| 项 | 实测值 | 影响 |
|---|---|---|
| CPU | 6 × Cortex-A78AE @ 1.728 GHz max, 单线程 | ARM，单核算力远低于桌面 x86，必须 SIMD/量化 |
| 指令集 | ARMv8.2-a：`asimd`(NEON) `asimdhp`(FP16) `asimddp`(DOTPROD int8) ✓ | DOTPROD 让 INT8 点积 4×加速；FP16 让半精 GRU 推理可飞 |
| 内存 | 7.4 GiB LPDDR5（CPU/GPU 共享） | RNNoise 权重 <1MiB 完全无压力；上大模型要克制 |
| GPU | Ampere SM 8.7（CUDA 13.2 / cuDNN 9.20 / TensorRT 10.16 全装） | 可给后处理大模型加速；对 RNNoise 是反向操作（PCIe 入口延迟 > 计算时间，不上） |
| DLA/PVA | **无**（Orin Nano 不挂） | 不能靠做 INT8 卷积加速器卸载 RNNoise |
| APE | tegra-ape ALSA 卡，20 × ADMAIF 已暴露 | Tegra 音频处理子系统，**核心硬卸载机会**（见 A.3） |
| 功耗 | 默认 25W 模式（`nvpmodel` 默认 1），`jetson_clocks` 需 root | 25W 下 CPU 满载仍能稳跑 RNNoise，不需要硬切更高档 |
| 内核 | 6.8.12 tegra oot，CONFIG_PREEMPT（非 RT），HZ_250 | 已是好基线；真要亚毫秒级上 PREEMPT_RT 是选项 |
| 音频后端 | PipeWire 1.0.5 + `libpipewire-module-rt` 已加载；quantum=1024（≈21.3ms@48k） | quantum 偏大，要减小；pw-cat/pw-top/pw-loopback 全可用 |
| 物理麦克风 | **当前没有外接麦克风**（只有 HDMI 输出 + APE 20 路虚拟 ADMAIF） | 端到端真机验证前必须先解决采集源 |
| 编译器 | gcc 13.3, cmake 4.3.4, 无 clang | 全套可用 gcc 即可；Clang+ Polly 是后续 ARM 矢量化优化选项 |

### A.2 可立即落地的优化（按收益排序）

1. **ARM NEON + DOTPROD + FP16 编译档**（最大收益，零代码）
   - 目标 CFLAGS：`-march=armv8.2-a+dotprod+fp16 -mtune=cortex-a78ae -O3 -ffast-math -funroll-loops -flto`
   - RNNoise 现有 RTCD 是 x86 SSE/AVX 路径；编译期需补 ARM NEON 路径或直接走通用 C 跑通后测内层瓶颈加 NEON。
   - 权重固化为 64B 对齐 `.rodata` + `__builtin_prefetch` 暖 L2（模型 <300KB，稳放 L2 1.5MiB）。

2. **PipeWire quantum 降到 480 samples（10ms@48k）**
   - 改 `/etc/pipewire/client-rt.conf` 设 `default.clock.quantum=480 default.clock.min-quantum=480`，与 RNNoise 一帧 480 samples 对齐，消除对齐浪费的缓冲。
   - 不能更低：要给 AEC/VAD 留观察点。

3. **实时调度**
   - module-rt 已启用；追加给预处理线程 `SCHED_FIFO` + `rtprio`，并 `mlockall(MCL_CURRENT|MCL_FUTURE)` 锁内存避免页错。
   - 用 `taskset` 把管线绑到 `CPU2-CPU3`，避开 NVIDIA 后台/系统核（GPU 中断往往在 CPU0）。

4. **不上 GPU 跑 RNNoise**
   - RNNoise 单帧 <10µs CPU，GPU 启动延迟（cudaLaunchKernel）十几 µs 起，反而劣化延迟并占显存带宽。GPU 资源留给后处理大模型 + TensorRT。

5. **`jetson_clocks`（需 root）锁满频**
   - 当前 governor=schedutil 有动态调频，对实时应用会引入随机毛刺。锁满 1.728 GHz 可让最坏情况确定化。`nvpmodel -m MAXN` 切至最大功率档。

6. **延迟基线工具链先建**
   - 用 `pw-top` 看消费/生产错位；自己 trace 用 `perfetto` 或 `lttng`（JetPack 自带 perfetto）把每 stage 时间戳画成图；以 1kHz 强 load 测最坏情况而非均值。
   - 这是后续所有优化的裁判。

### A.3 高级/远期：利用 Tegra APE 硬件卸载

`tegra-ape` ALSA 卡不是普通 HDA 声卡——它是 Tegra234 的 **APE（Audio Processing Engine）**，包含 ADSP 子系统 + 硬件引擎（AFC/MVC/EQ/DRC/OPE/AMX/ADX/IQC 等）。理论上：

- 把预处理管线里 **EQ/AGC/AEC** 这类线性/FFT 运算放到 APE 硬件引擎，CPU 完全解放给 RNNoise。
- 把 RNNoise 整体挪到 ADSP 固件，让 CPU 接近空载——这条吻合"未来搬到硬件/FPGA"的方向（先搬到 ADSP 比 FPGA 离用户更近、迭代更快）。

代价：APE 的固件开发是 NVIDIA BSP 范畴、文档封闭，需要 NVIDIA NvAudio SDK 访问。归为 **Phase 3 远期目标**，现阶段只把它列入路线图：DSP 化 → ADSP → FPGA。

### A.4 平台无关项

- 当前机不接物理麦时，验证路径：用 `pw-loopback` 把一个 sink 输出回环成 source 当"假麦克风"，先打通管线逻辑，再补硬件。
- APE ADMAIF 20 路是裸数字接口，不会替你采模拟音；要用本机录音必须接 USB 麦克风。
- `module-loopback`、`module-filter-chain` 当前未加载，方案 A 实施时需要手动 `pw-cli load` 或写配置。

## 附录 B：FPGA 接入架构

### B.0 前提（先说清楚）
Jetson Orin Nano 内置 APE（ADSP + 硬件音频引擎）+ Ampere GPU，理论上算力足够跑完预处理。所以 FPGA 不是性能补丁，而是**确定性 + 异构卸载 + 产品化声卡**的载体。建议路线顺序：

```
 SW RNNoise(Jetson)   →  ADSP 固件(APE)  →  FPGA SoC 子卡
   Phase 0                Phase 1              Phase 2
"先可改通跑"             "离硬件最近一跳"       "独立声卡枚举"
```
绝大部分软件接口经 Phase 1 演练后，Phase 2 到 FPGA 的迁移代价才会显著低于从 SW 直冲。

### B.1 接入拓扑选项对比（"最舒服"判定）

| 拓扑 | 互联 | 延迟量级 | 双向 | Jetson 见到的形态 | 舒适度 |
|---|---|---|---|---|---|
| **A. 协处理器** | PCIe DMA / 驻留内存 + doorbell | 5–10 µs DMA + 1 帧算法 | ✅ | 一段 PCI BAR0 加速器，Jetson 像调 XILINX XRT 一样调 | 中 |
| **B. 流式前端** | I²S/TDM 直连 Jetson APE 的 ADMAIF | 1 帧 (10ms) + 线锁同步 | ✅ 单工足够 | 一张 ALSA 卡（PipeWire 把它当物理麦克风） | **最舒服** |
| **C. TSN/AVB 以太网** | 1588 PTP 同步的 L2 帧流 | 1 帧 + 1588 校正 | ✅ | 一条 RTP/Media 协议源，需软件解码 | 中低 |
| **D. SPI/QSPI** | 4 线 SPI | 帧每秒 50, CPU 中断频繁 | 双工 | 字符设备 → 用户态库 | 低（开发易但抖动坏） |
| **E. USB Class Audio (UAC)** | USB ISO | 1 ms ×N 帧，跨 host 调度毛刺 | ✅ | 直接当 UAC 麦克风 | 开发易、实时性差 |

**"最舒服"= 拓扑 B（I²S/TDM 流式前端）**：
- FPGA 是 master/slave（word-clock）链路，采样与帧边界**由音频时钟本身驱动**，与软件时钟完全解耦——这是"声卡"语义，而不是"加速卡"语义。
- Jetson 端**零代码**路由：FPGA 输出的 I²S 出现在 `tegra-ape` 的某条 ADMAIF/ASRC 通道上，PipeWire 自动认作 source，省掉我们写驱动/快速路径。
- Latency 上界 = 1 帧 × 480 samples × (1/48k) ≈ 10 ms；如把 RNNoise 改为 sample-streaming 流水线可实现 sub-frame，但当前帧粒度足够。
- 调试时可断开 I²S 走 PCIe 回环，互不影响。

### B.2 推荐落地形态：FPGA = 完整音频前端（含采集）+ I²S master，Jetson = 下游 host

输入侧在 FPGA 上——这是产品形态的决定：FPGA 子卡本身**就是一张"AI 麦克风声卡"**，从模拟/数字麦到 RNNoise 都在 FPGA 内完成，Jetson 只做下游 host + 后处理。

```
 ┌─────────────────── FPGA 子卡（"AI 麦克风硬件产品"）───────────────────┐
 │                                                                      │
 │  [MEMS 麦阵列]    或    [模拟麦/动圈] + ADC                            │
 │   PDM 数字麦直入                                  I²S/PCM 数字麦直入   │
 │      │ PDM                                     │ 模拟 → ADC (PCM1808/ │
 │      ▼                                         │   CS53L21/AK5394)   │
 │  ┌──────────┐  多路 mux/decim                  ▼                     │
 │  │PDM→PCM   │─────────────→ [多路 TDM/I²S bus]                        │
 │  │(CIC+FIR) │                       │                                 │
 │  └──────────┘                       ▼                                 │
 │                          ┌──────────────────────────────┐             │
 │                          │  可选:波束形成/DOA/MM        │             │
 │                          │  (在 FPGA 内做, 离声源近)    │             │
 │                          └──────────────┬───────────────┘             │
 │                                          ▼                            │
 │                          ┌──────────────────────────────┐             │
 │                          │  RNNoise kernel (定点硬件实现)│             │
 │                          │  8-bit 权重固化 ROM            │             │
 │                          │  ERB gain + deep filter       │             │
 │                          └──────────────┬───────────────┘             │
 │                                         ▼                            │
 │                          ┌──────────────────────────────┐             │
 │                          │  轻量 AGC / DRC (可选)         │             │
 │                          └──────────────┬───────────────┘             │
 │                                         │                            │
 │  [板上晶振 12.288 MHz / 24.576 MHz] → [FPGA PLL] →  MCLK               │
 │            直接产生  BCK / LRCK                                       │
 │                                         │                            │
 │          [I²S/TDM OUT]  ← master ───────┘                            │
 └─────────────────────────────────────────┼────────────────────────────┘
                                           │ I²S: MCLK/BCK/LRCK/DATA
                                           │ (FPGA 当 master)
                                           ▼
 ┌── Jetson APE (ADMAIF/I²S block) ──────────┐
 │ I²S slave-in → ADMAIF 通道                │
 │              ↓ ALSA /dev/snd/pcmC1D0c      │
 │              ↓ ALSA → PipeWire (source)    │
 │              source "FPGA-AI-Mic"           │ → Mozart 预处理剩余模块
 │                                            │   (AEC/AGC/VAD/打标)
 └────────────────────────────────────────────┘
```

要点：

- **FPGA 当 I²S/TDM master，Jetson APE 当 slave 收 PCM**：因为音频时钟的真正源头在 FPGA 板上的麦阵列晶振，让源头当 master。Jetson APE 直接锁到外部 I²S 时钟上（tegra234 的 I²S 接口可配外部 master 模式），这反而比让 Jetson 自己当 master 更"声卡"——host 跟音频时钟走，不是音频时钟跟 host 走。
- **Jetson APE 的 ASRC** 仍留着做兜底——若 FPGA 与 Jetson 系统时钟有缓慢漂移，APE 的 ASRC 把它驳到 48k 严格量子上，PipeWire quantum 仍稳定。这是 jetson 端唯一可能做的"驯化"步骤。
- **FPGA 不暴露 PCIe / 不挂门铃**：Jetson 这一侧看不到"加速器"，只看到一张声卡。软件路径清一色 ALSA→PipeWire→virtual source 那套，与附录 A 的方案一致。Jetson 端代码对 FPGA 存在与否没有耦合。
- **RNNoise 在 FPGA 上的实现**：用 `rnnoise_data.c` 既存的 8-bit 量化权重 → 直接照搬为 Verilog LUT ROM；GRU 是小 RNN（22→~128→~4），整网 <300 KB；推荐工具链顺序：
  1. C reference fixed-point → bit-accurate C 模型（与原 RNnoise 比对样本）
  2. HLS（Vitis HLS / hls4ml / FINN）综合到 RTL；或手写流水线也行
  3. 流水线推进：Bark/ERB 特征 → GRU → mask 增益 → 帧重叠相加全部数据流化，吞吐随采样率自然匹配
- **不对 AEC 做硬搬**：现代 AEC3 自适应步长对定点/截断敏感，留到 Jetson CPU 用 WebRTC；FPGA 只做输入侧线性均衡 + RNNoise。后期可把 AEC 推回 ADSP。
- **波束形成/DOA 可在 FPGA 内做**：因为输入侧就在 FPGA，多麦波束形成在硬件里做比送 Jetson 再处理更合适——离声源近、避免引入 Jetson 数字 IO 抖动，也是 FPGA 子卡的差异化点。

### B.2a 输入侧落在 FPGA 后的额外决策

| 项 | 选项 | 推荐 | 理由 |
|---|---|---|---|
| 麦类型 | PDM 数字 MEMS / I²S 数字 MEMS / 模拟麦 + ADC | **PDM 数字 MEMS**（SPH0641LU4 / ICS-43434 等） | PDM 解码（CIC + 半带 FIR）在 FPGA 近乎免费；省 ADC 芯片与走线；电平已数字，抗扰强 |
| 路数 | 1 / 2 / 4 / 8 | 2 起步、4 高配、8 产品 | 2 用于 AEC 双麦参考，4+ 可上波束 |
| 主时钟源 | FPGA PLL 自振 / 外部 TCXO | **外部 TCXO 12.288 MHz** | PLL 自振 jitter 抖动大；TCXO 给音频时钟严格稳定性，决定 SNR |
| 是否带 AFE 电源 | 独立 LDO / 系统供电 | 独立 LDO + 分离地 | 麦阵列对数字噪声敏感，远离 Jetson 高速 IO 电源域 |
| 板形态 | Pmod/HAT 子卡 / 自板 | 起步用 Pmod，定型做自板 | 起步验证走 Pmod 快；产品走自板集成成本最低 |
| 是否在 FPGA 端做"软声卡枚举" | I²S only / I²S + USB-CDC 控制通道 | **I²S + 一根额外 UART/SPI 控制通道** | 用来传元数据(VAD flag/segment_id/conf)与配置切换，跟音频流分开走，不打扰 I²S 时钟 |

### B.3 与管线架构的接口契约（最关键的"舒服"点）

接入 FPGA **不动主管线契约，只换一个 Stage 的实现**：

```rust
// mozart-core 里已有的 Stage trait
pub trait Stage {
    fn process(&mut self, in_frame: &[f32], meta_in: &FrameMeta) -> Result<FrameBuf>;
}

// 三种实现,配置切换
enum DenoiseBackend {
    SoftwareRnnoise,   // Phase 0
    TapeApeAdsp,       // Phase 1
    StreamingFpga,     // Phase 2 (退化为 no-op Stage,因为真正处理在 FPGA)
}
```

Phase 2 时 `mozart-pre` 把 `DenoiseBackend::StreamingFpga` 选成 `IdentityStage`（直接 copy）——因为 RNNoise 已经在 FPGA 端做了，Jetson 端管线里只剩 AEC + AGC/VAD/打标。同时 Jetson 通过 ALSA 收到的是 FPGA 送来的 PCM（已清净），通过另一根 UART/SPI 通道收 VAD flag 等 FPGA 端已算好的元数据。这种"采收进来已净化"的语义是拓扑 B 才有的，拓扑 A 做不到。这正是 B 的"舒服"核心：**架构分层：业务逻辑以 Phase 0 形态写好，Denoise trait 永远只看见"输入脏帧 → 输出净帧"，FPGA 与否用户视角无差。**

### B.4 数据通路具体参数

| 项 | 值 | 备注 |
|---|---|---|
| 采样率 | 48 kHz | RNNoise 原生 |
| 帧大小 | 480 samples (10 ms) | 与 PipeWire quantum=480 对齐 |
| 音频通路 | I²S/TDM 数据线 | 多麦可在 FPGA 内 mux 后用 TDM-8 一线送 |
| 位宽 | 24-bit（信息保真） / 16-bit（精简） | I²S 标准；定点后送 f32 给 Jetson AEC/VAD |
| 元数据通路 | 独立 UART/SPI（不夹在 I²S） | 传 VAD/segment_id/conf，不污染音频时钟 |
| 附加延迟 | ≈ 10 ms（1 帧 RNNoise 量化窗） | 心理感知无关，与 SW 版同档 |
| Clock | **FPGA PLL + 外部 TCXO 12.288 MHz → MCLK → BCK/LRCK（FPGA master）** | Jetson APE 配为 I²S external master，ASRC 兜底抗漂 |

### B.5 走 Phase 0→1→2 才"舒服",别跳级
- **别先写 FPGA 再想驱动**。先在 Jetson CPU 把整条管线 + trait 跑通，把契约固化 → Phase 0 已在 §10。
- Phase 1 在 Jetson APE 的 ADSP/NvAudio 上把 RNNoise 变成 BSP 加速器，信号链不变。验证定点量化误差、负荷迁移。
- Phase 2 把同一定点比特模型搬到 FPGA 子卡，Jetson 侧只换：① 收音源切到 I²S 卡 ② DenoiseBackend enum 改值。其他代码 0 行改动。

### B.6 容易踩坑的事（输入侧在 FPGA 后）

- **Jetson I²S 必须能配为 external master 模式**：tegra234 的 I2S 块支持外部 bit-clock / frame-sync slave 模式。Stock overlay 默认 Jetson 当 master（ReSpeaker overlay 在 fragment@3 显式写 `bitclock-master; frame-master;` 挂在 Jetson endpoint 上）。我们要反向时，写自定义 dtbo 在 `hdr40_snd_i2s_dap_ep` 上写 `bitclock-slave; frame-slave;`（参见 §B.8 实测）。
- **MCLK 物理连接 — 原担心不成立**：实测四份现成 audio overlay（adafruit-sph0645lm4h / -uda1334a / -respeaker-4-mic-array / -fe-pi）**无一把 MCLK 走 40-pin 外引脚**；所有 codec 都从 BCLK 通过自身的内 PLL 自产 MCLK。tegra234 I2S 块也支持从外部 BCLK 反推音频时钟。所以 FPGA master 形态只需要 BCLK/LRCK/DIN 三根外露，**不需要专门打 MCLK 走 40-pin**。原"必须物理 MCLK 连接"那条作废。
- **clock drift**：FPGA TCXO 与 Jetson 系统时钟有缓慢漂移，但都在 Hz–数 kHz 量级；以 ASRC 兜底即可，不要试图做软件让位 audio clock 的复杂同步。
- **backpressure**：Jetson 一旦卡 PipeWire 处理慢 underrun，I²S master 不会等你——会丢帧。从 FPGA 角度看 Frame Check 连续计数是唯一手段。务必在 ALSA source 上跑 `pw-top` 观察错位。
- **PDM MEMS 的 SNR**：低端 PDM 麦 SNR ~63dB，明显低于模拟麦+好 ADC（~96dB）；人声够用，做"高保真AI麦克"应选 I²S 数字麦（如 ICS-43434 / SPH0645LM4H 已是后者）。
- **多麦相位对齐**：同一批 PDM 麦的群延时差可能达到几十 µs，对 AEC 与波束致命；FPGA 端必须做相位对齐（同 batch 同步复位）。

### B.8 实测：Jetson 40-pin audio 引脚 + stock overlay 兼容佐证

实测 `/proc/device-tree` 与 BSP 自带 dtbo（路径 `/boot/*.dtbo`）。核心结论：

#### B.8.1 40-pin header 的 I²S 物理映射（确认可用）

四份 stock overlay 一致把 audio 引到这四根 40-pin：

| 40-pin | SoC pad | overlay `nvidia,function` | 用途 | 默认方向 |
|---|---|---|---|---|
| pin 12 | `soc_gpio41_ph7` | `i2s2` | LRCLK / FSYNC | input |
| pin 35 | `soc_gpio44_pi2` | `i2s2` | BCLK | input |
| pin 38 | `soc_gpio43_pi1` | `i2s2` | DIN (外部 → Jetson) | input (tristate) |
| pin 40 | `soc_gpio42_pi0` | `i2s2` | DOUT (Jetson → 外部) | output |

挂在 `bus@0/aconnect@2900000/ahub@2900800/i2s@2901100`，别名 **`hdr40_snd_i2s_dap_ep` = `tegra_i2s2`**。I2S1 是 SoC 内 HDA 连接（HDMI 音频），I2S3..6 在 base dtb 里 `status="disabled"`；**I2S2 在 base dtb 里 `status="okay"` 已可启用**，`assigned-clock-rates = 0x177000 = 1.536 MHz`（= 48k × 32bit stereo），`dai-format="i2s"`，等外部 overlay 接 codec 即用。

#### B.8.2 BSP 自带 audio overlay，本板已兼容

`p3768-0000+p3767-0005-super`（你这块 Orin Nano Super）出现在每个 audio overlay 的 `compatible` 列表里——意味着这些现成板可以在你板子上一键 `dtbo` 加载。挑两个对我们最有价值：

| 现成硬件 | dtbo | 形态 | 对 Mozart 阶段价值 |
|---|---|---|---|
| **Adafruit SPH0645LM4H** PDM MEMS 麦 ×1 | `audio-adafruit-sph0645lm4h.dtbo` | 我期初推荐的型号；I²S 麦数据库走 BCLK 内 PLL 自产 MCLK；只用 pin 12/35/38 | **Phase 0 跑通验证**：不用做板，买 ~$6 麦 + 杜邦线即可变成声卡类比 |
| **ReSpeaker 4-Mic Array** (AC108) | `audio-respeaker-4-mic-array.dtbo` | 4 麦 + I²C codec + TDM (`dai-format="dsp_a"`)；`bitclock-master/frame-master` 挂 **Jetson endpoint**，即 Jetson 是 master | **原型多麦验证**：测 AEC/波束相位对齐；可现成证伪 `nvidia,bitclock-master` 的 master/slave 反向设置 |

#### B.8.3 FPGA = master 形态：需要的最小自定义 dtbo

把 stock ReSpeaker overlay 反向写一份——做这些改动：

```dts
fragment@1 {
    target = <&hdr40_snd_i2s_dap_ep>;
    __overlay__ {
        dai-format = "i2s";       /* 或 "dsp_a" 走 TDM */
        bitclock-slave;            /* 关键：把 Jetson 设为 BCLK slave */
        frame-slave;               /* Jetson 当 FSYNC slave */
        remote-endpoint = <&fpga_dac_ep>;
    };
};
```
对比 ReSpeaker stock：
```dts
bitclock-master; frame-master;     /* Jetson 当 master */
```

外加把四根 pinmux 改成 input/BCLK/LRCK/DIN（参照 adafruit-sph0645lm4h overlay 的 Pin 接收侧）。BCLK、LRCK 由 FPGA 板 TCXO + FPGA PLL 驱动，DIN 把 RNNoise 已清净的 PCM 送给 Jetson。

#### B.8.4 不需要 MCLK 物理引出（实测结论）

反复交叉看四份 stock overlay——**没有任何 overlay 把 MCLK mux 到 40-pin 任何脚上**。SGTL5000 在 fe-pi overlay 里 declared 了一个 `fixed-clock` 名为 `sgtl5000_mclk` 频率 `0xbb8000 = 12.288 MHz`，但只是给 codec driver 的时钟标志，物理上 codec 仍是从 BCLK 自产 MCLK；I2S2 block 也支持 BCLK external master 模式。所以我们的 FPGA master 形态只要 BCLK/LRCK/DIN 三根，**MCLK 在 40-pin 上不存在，也不需要它存在**，原 B.6 那条坑作废。

#### B.8.5 元数据旁路仍需要对外管控通道

I²S 不能传元数据（帧时钟主持音频流）。把元数据走 40-pin 上闲置的 UART (`/dev/ttyTHS*` 或 `/dev/ttyS0`) 或 GPIO+SPI；推荐 UART，因为 stock `nvidia,tegra21x-uart` 已就绪，且 UART 不受音频时钟抖动牵连。可挂一个 FPGA 上软核（Vex/RISC-V）当 USB-CDC/UART 端点，把 VAD_flag/segment_id/conf 框成一条 KVP 协议。

### B.7 候选硬件清单（仅供你后面采购参考）
- 入门: Gowin/Lattice ECP5 + SPH0641LU4 PDM MEMS ×2-4 + TCXO 12.288 MHz；I²S 跑通概念很便宜。
- 主流: AMD/Xilinx Artix-7 / Spartan-7 SoC，Vitis HLS 综合 RNNoise；FINN 框架支持量化 INT8 推理。
- 生产: Zynq UltraScale+ MPSoC——PS 端跑控制路径（元数据 UART/SPI 协议、配置、状态），PL 端做 PDM 解码/波束/RNNoise/I²S master；可一路演进到"AI 麦克风硬件产品"。

## 附录 C：目标硬件对接（EBF ZYNQ7020 BTB，纯 PL 形态）

资料：`ref/ZYNQ_BTB_Hardware_Answers.md`（你已整理）+ `ref/EBF_ZYNQ70xx_BTB_CORE/` + `ref/EBF_ZYNQ70xx_BTB_Plate/` 原理图 PDF。

实物 = 核心板（EBF410020，**XC7Z020-2CLG400I**）+ 底板（EBF410021），240-pin BTB 夹层。

### C.0 核心约束：只用 PL，PS 仅做 boot

用户决策：**上电加载完 bitstream 之后就是纯 FPGA 的东西**。这把整张板的形态收敛成"纯 FPGA 加速卡"，PS 视为哑后端，加载完就当它不存在。由此推出：

1. **PS I²C1 / PS UART1 一律不用** → WM8960 配置和元数据旁路都要换方案。
2. **WM8960 寄存器配置由 PL 内 I²C master state machine 在上电序列中写入**（Bitstream 内固化一张寄存器表，PowerOn 上电自动按序刷写到 WM8960）。
3. **元数据旁路整体取消** → Jetson 端自行用轻量 VAD（Silero VAD，下一阶段引来）算 VAD_flag/segment_id/conf，FPGA 只负责把"已清净的音频流"送给 Jetson。FPGA 这边不再有任何 KVP/UART 控制通道。
4. **bitstream 加载**走原有 PS BootROM/QSPI 路径——就让 PS 在启动早期完成 bitstream 加载，PS 任务到此为止，之后 PL 跑自己的世界。PS 仍上电但完全空闲，**不需要在 PS 上跑任何 Linux/裸程序**。
5. **bitstream 一旦加载完成，PL 侧对外只需要 5 根线**：I²S 的 MCLK（自用，不到 Jetson）+ BCLK + LRCLK + ADCDAT（→Jetson）+ DACDAT（←Jetson，留 AEC 回 path）。

### C.1 现有板信号链（纯 PL 视角，WM8960 codec 不变）

```
  [3.5mm 麦 J12/J13]                                  (板上已布)
         │ 模拟 + MICBIAS1 偏置（WM8960 自带）
         ▼
  [WM8960 codec]   ──I²C 控制──►  ZYNQ PL 自实现的 I²C master（不再走 PS I2C1）
    │ ADC + 耳放 + Class-D
    │
    │ I²S：ADCLRC/DACLRC/BCLK/ADCDAT/DACDAT + MCLK
    ▼
  [ZYNQ PL Bank 34, 3.3V HR]
    V12 / W13 / V15 / W15 / R19 / P19
    │
    ├── 阶段 1（验证,bitstream V1）：
    │     PL 内只做 I/O loopback：上电序列刷 WM8960 寄存器，再让 WM8960 的 I²S ADCLRC/BCLK/ADCDATA
    │     经 PL 内部直通到对 Jetson 的 3 根（BCLK/LRCLK/DIN）。**不跑 RNNoise**
    │
    └── 阶段 2（产品,bitstream V2）：
          PL 内插 RNNoise pipeline  ①收 WM8960 I²S  → ② Ovr/Overlap
             → ③ ERB 特征  → ④ 定点 GRU  → ⑤ 增益 mask  → ⑥ I²S master 出
          Jetson 看到的还是一张 I²S 麦克卡，只是音频已被 FPGA 端洗净
```

差异要点（vs §B.2 设想）：

| 项 | §B.2 设想 | EBF 实际（纯 PL） | 处置 |
|---|---|---|---|
| 麦类型 | PDM 数字 MEMS | 3.5mm 模拟驻极体 | WM8960 内 ADC 转 I²S PCM，不另摆 PDM 麦 |
| PDM 解码 | 在 FPGA 内做 | 不需要 | **省掉 §B.2a 的"FPGA PDM CIC+FIR"那块工作** |
| codec 配置 | 不需要 / PS 配 | **必须 PL 内 I²C master** | bitstream 内固化寄存器表，上电自动写 |
| MCLK | 留作 free | 板上已连到 WM8960(W15) | PL 内部生成 12.288 MHz 喂 WM8960 |
| 元数据旁路 | UART | **取消** | Jetson 端用 Silero VAD 自己算 VAD_flag |
| 控制接口 | UART/I²C 都用 PS | 全免（bitstream 固化即配置） | 整板 0 运行时控制 |
| 麦路数 | 推 2/4/8 | WM8960 立体声 ADC | 单麦起步；AEC 远端参考走 DACDAT 回 path |
| 字宽 | 24-bit | WM8960 吃 24-bit | 直兼容 |

### C.2 PL 内 I²C master 上电序列（bitstream 自含）

关键时序与寄存器表（WM8960 默认 I²C 地址 `0x1A`，7-bit）：

```vhdl
-- pseudo
power_on:
  wait 100 ms;              -- 等 WM8960 Vref 稳
  i2c_write(0x1A, 0x0F, 0x0000);  -- write sequence: active control, 不 alloc
  i2c_write(0x1A, 0x19, 0x00F8);  -- power MGMT1: CONTROL2 + INL+R + OUTL+R + HPL+R
  i2c_write(0x1A, 0x1A, 0x01F0);  -- power MGMT2: SPKL+R + MICEN
  -- ...专项配表见 assets/wm8960_init_spectrum.txt（待写）
  i2c_write(0x1A, 0x30, 0x0041);  -- IFACE: 24-bit, I²S mode, 48kHz
  i2c_write(0x1A, 0x04, 0x0050);  --与时钟相关；按 12.288MHz 配
  -- 完成，INPL/MICBIAS1 已有，采音可以发
```

- 这一段不需要 PS 参与。Xilinx 的 STARTUP 原语配合 INIT_DONE → 触发 PL 内 sm。
- bitstream 里放一张 144 项 init_table（i²c addr/reg/data 三字节），sm 按序发出，几百周期完事。
- 不在文档里抄全表，固化在 hls/FINN 综合前给的 `.mif`/`.coe`，待写 assets 目录。

### C.3 EBF 板与 Jetson 的物理连接

用户原话："板子和 Jetson 连接我还没想好"。只剩两种合理候选（原 C.3 USB 已被排除，因为走 PS）：

| 方案 | 形态 | 优点 | 缺点 | 推荐场景 |
|---|---|---|---|---|
| **C.3.a 飞线** | 从 EBF 底板 BTB 暴露的 PL Bank 34 4 根 I²S 脚 (P19/R19/V12/V15) + GND,飞线到 Jetson 40-pin | 不需设计 PCB,几小时打通 | BCLK 3.072 MHz 长飞线串扰,量产不行 | 实验室验证阶段 |
| **C.3.b 专用转接板** | 设计半长 PCB，一边接 EBF BTB 扩展座(SGDBF-05-60P-H30)，一边作为 HAT 插 Jetson 40-pin | 阻抗匹配，量产一致 | 要画 PCB,周期 1-2 周 | 产品化 |

建议：**先用 C.3.a 飞线打通管线（阶段 1 I/O loopback bitstream），追踪到 PipeWire source 听到麦声后再做 C.3.b 转接板进阶段 2**。

### C.4 引脚映射（C.3.a / C.3.b 通用）

| 信号 | EBF ZYNQ PL 引脚(Pkg) | 方向 | Jetson 40-pin | SoC pad | dtbo 处理 |
|---|---|---|---|---|---|
| I2S BCLK | R19 (Bank 34) | FPGA → Jetson | pin 35 | soc_gpio44_pi2 | `nvidia,function="i2s2"`,pull-up,input |
| I2S LRCLK | V12 (AUDIO_ADCLRC) | FPGA → Jetson | pin 12 | soc_gpio41_ph7 | 同上 |
| I2S DIN | P19 (AUDIO_ADCDAT) | FPGA → Jetson | pin 38 | soc_gpio43_pi1 | input,tristate |
| I2S DOUT（AEC 远端参考） | V15 (AUDIO_DACDAT) | Jetson → FPGA | pin 40 | soc_gpio42_pi0 | output |
| MCLK | W15 | FPGA 自用（喂 WM8960） | — | — | 不引到 Jetson |
| GND | 任 GND | | 40-pin GND（pin 9/25/39） | | |
| 电平 | Bank 34 HR 3.3V LVCMOS33 | | Jetson 40-pin 3.3V | | **直连兼容，免电平转换** |

- Jetson I²S2 必须改为 slave 模式（原 stock overlay 把 Jetson 设 master，自定义 dtbo 改之，§B.8.3）。
- MCLK 不出板（见 §B.8.4 实测）；WM8960 板内 MCLK 由 PL 提供即可。
- **元数据旁路整条废弃**：Jetson 侧只用 ALSA source 接 PCM，缺点是 Jetson 重算 VAD/segment_id/conf（Silero VAD，~0.2 ms/帧，可接受）。后处理契约的元数据字段照产生，只是来源由"FPGA 算好送过来"改成"Jetson 自己算"。
- **PL 端 UART/IP 不做**：bitstream 加载完，PL 对外只剩 I²S 那 4 根线（不含 MCLK）+电源+GND。

### C.5 立即可做的无依赖动作（按这条线推进）

1. **Vivado 工程骨架**：建 PL-only 工程，约束 Bank 34 4 根 I²S 输出 + 1 根 DOUT 输入为 LVCMOS33。
2. **PL 内 I²C master state machine**：写一个简易 sm，自带 wm8960 init table 在 INIT_DONE 之后按序提议给 WM8960 I²C signal。验证：示波器看 WM8960 LROUT/HP 有正弦，或看 J13 耳机听得见 3.5mm 麦的回放。
3. **I/O loopback bitstream**：PL 把 WM8960 来的 ADCDAT 接到对 Jetson 的 DIN，BCLK/LRCLK 由 PL 提供，Jetson 收 48k/I²S/24-bit 的 PCM。
4. **Jetson 侧 dtbo**：配 §B.8.3 那条自定义 dtbo（`bitclock-slave; frame-slave;`），用 `pw-record` 录一段 3.5mm 麦的声，验证打通。
5. 全部通路打通后，**才**插 RNNoise pipeline 到 PL 内（替换第 3 步的 loopback 位置）。

### C.6 当 RNNoise 可上线时的资源占用粗估（XC7Z020，纯 PL）

XC7Z020 资源：LUT 53.2K / FF 106.4K / DSP48 220 / BRAM 630 Kb。
RNNoise（8-bit 定点 + MAC 流水线展开）粗占：
- LUT ≈ 7-10K（含控制 + I²C master + I²S master + ERB 特征），FF 5-8K
- DSP48 ≈ 35-50（MAC 密集）
- BRAM ≈ 50-80 Kb（权重 + 双帧缓冲 + 历史 3 帧 latency）
- 占 7Z020：~15-20% LUT，~20-23% DSP，~12% BRAM
- I²C master 几乎零占（LUT <100）
- 加上 I²S master + 双口 audio FIFO ≈ 1K LUT、3 DSP。

**结论**：XC7Z020 容下"WM8960 配置 + I²S master + RNNoise + 元数据旁路（已废弃）+ AEC 回 path"完全足够；后期加 4-8 麦波束形成可能要升到 7Z030 量级，但目前阶段不在射程内。