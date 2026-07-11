# AI 声卡 · 预处理阶段设计文档（Jetson-only）

> Project Mozart · 预处理设计 v0.1（Jetson 纯软件版）
> 后续 FPGA 版见 `docs/preprocessing-design-fpga.md`

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

FPGA 接入架构（附录 B）与 ZYNQ 硬件对接（附录 C）已移至独立文档 `docs/preprocessing-design-fpga.md`。当前阶段聚焦 Jetson 纯软件管线。