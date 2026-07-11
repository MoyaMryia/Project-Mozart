# AI 声卡 · 预处理阶段设计文档

> Project Mozart · 预处理设计 v0.1
> 范围：仅预处理阶段。后处理阶段另行设计，本期只为其预留契约接口。

## 1. 设计目标

把"声卡"抽象成两级解耦流水线：

- **预处理**：把脏采集流洗净、归一化、分段标注，产出一个**有契约保证**的标准流。
- **后处理**（后续）：在干净流上挂复杂 AI 能力，无需再做任何清洗。

预处理要做到三件事：

1. **Causal 实时**：所有模块只依赖过去和当前样本，不允许整段回看（混合模式约束）。
2. **速度优先**：在不丢质量的前提下压低 CPU 与延迟，单帧端到端预算 ≤ 5 ms（见 §7）。
3. **可插拔去噪**：去噪是预处理里最重的一块，必须能按档位切换模型。

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
| ⑤ | 去噪 | 残差帧 | 增强帧 | trait,可换 RNNoise/DeepFilterNet |
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

实现可选：
- **A. 复用现成**：直接用 DeepFilterNet 的 LADSPA 插件 + PipeWire filter-chain 配置（见其 `ladspa/README.md`，最小延迟 20ms STFT + host）。零开发快速验证。
- **B. 自主管线**：我们进程用 `pipewire-rs` 同时申请 `capture` 流 + 注册 `virtual source` 流，中间跑 §2 全管线。长期路线。

建议：先用 A 跑通端到端验证 PipeWire 虚设备确实能被其它应用当麦克风采，再用 B 替换管线。Windows 走 WASAPI loopback 同思路。

## 6. 去噪模型对比与选型建议

去噪是预处理最重模块，单独评估。

| 维度 | RNNoise | DeepFilterNet | CleanUNet | PercepNet | FullSubNet+ |
|---|---|---|---|---|---|
| 频带 | 48 kHz 全带 | 48 kHz 全带 | 48 kHz | 48 kHz | 16/48 kHz |
| 因果实时 | 是 | 是(LADSPA 版无 lookahead) | 是(需GPU) | 是 | 否(双向) |
| 单帧 CPU | 极低(<10 µs) | 低(~1-2 ms) | 中(GPU) | 低 | 高 |
| 算法延迟 | <10 ms | 20 ms(STFT) | 取决帧 | ~20 ms | 大 |
| 最低端到端 | ~10 ms | ~20-25 ms | 需GPU | ~20 ms | 不适合 |
| 质量(DNS类) | 中 | 中上 | 高 | 中上 | 高 |
| 语言 | C | Rust+libDF+C ABI | PyTorch | C(移植) | PyTorch |
| 集成成本 | 极低(纯C,零依赖) | 低(C ABI现成) | 高(需Torch推理) | 中(需移植) | 高 |
| License | BSD-3 | MIT/Apache 双 | MIT | BSD-like | MIT |
| 维护 | xiph活跃,2024 v0.2,2025数据更新 | v0.5.6(2023.8)稳定 | NVIDIA | 实验性 | 学术源码 |
| PipeWire现成 | 需自己包 | **自带LADSPA+filter-chain配置** | 无 | 无 | 无 |
| 额外能力 | 内置VAD状态 | postfilter,残差控制 | 无 | 不可逆 | 无 |

### 选型建议：双档可切换
- **默认档（速度优先）= RNNoise**：纯 C、零依赖、亚毫秒级、xiph 维护、可动态加载 little/标准两种权重大小模型。极致"运行速度越快越好"。
- **高质量档 = DeepFilterNet**：Rust 原生 + 暴露 C ABI（cbindgen），契约融合；质量明显更高；最大代价是 20ms STFT 算法延迟（混合模式预算内可接受）。
- 设计一个 `Denoiser` trait，运行时按配置切换；不二选一。

理由对应你的约束：
- "速度越快越好" → RNNoise 兜底。
- "C/C++/Rust 混着来" → 两者都是 C ABI，自然混用。
- "想先研究再拍板" → 两个都做接口，后期可真机对比 AB。

### 我倾向的默认
开箱默认 **RNNoise**（满足速度优先）；提供一行配置切到 DeepFilterNet。等你跑 AB 主观后，再把"默认"重定为质量更高那档。**最终拍板权在你跑完对比后。**

## 7. 延迟预算（单帧 20ms 端口）

| 阶段 | 预算 | 说明 |
|---|---|---|
| I/O 等待 | 20 ms | 一个端口周期 |
| 归一化 | <0.1 ms | 重采样因果 |
| 瞬态抑制 | <0.1 ms | 检测门 |
| AEC | ~0.2 ms | WebRTC AEC3 |
| 去噪 RNNoise | <0.5 ms | 见 §6 |
| 去噪 DFNet | ~1-2 ms | 高质量档 |
| 去混响 | <0.3 ms | 轻量 |
| AGC+VAD | <0.1 ms | |
| **总计(RNNoise)** | ≈ 21 ms | 算法延迟≈1ms |
| **总计(DFNet)** | ≈ 21-22 ms | 额外STFT 20ms缓冲 |

目标：从声卡回调到我们 virtual source 可读，端到端额外延迟 **RNNoise 档 ≤ 5ms,DFNet 档 ≤ 25ms**。

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

1. 拍板去噪默认档（建议先 RNNoise，AB 后再定）。
2. 确认走方案 A（先跑通 PipeWire 虚设备）还是方案 B（直接自主管线）。
3. 决定是否冻结 §4 契约（冻结后后处理设计可并行启动）。