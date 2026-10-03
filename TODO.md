# TODO

> 2026-08-28 板上实测后落盘，2026-08-30 预处理重写后更新，2026-10-02 同步审计结论。所有数字均在 Jetson Orin Nano Super 8GB（MAXN_SUPER 满频）实测，非估算。
> 配套文档：TARGET.md（定位）、DESIGN.md（设计）。低延迟 C++ realtime profile 已完成并验收；本文件只管"接下来做什么"。
> 2026-10-02 审计全文见 [reports/next-steps-audit-20261002/AUDIT.md](reports/next-steps-audit-20261002/AUDIT.md) 与 [reports/pr-2-review-20261002/REVIEW.md](reports/pr-2-review-20261002/REVIEW.md)。

---

## 0. 一句话现状

RVC 普通 file/quality 路径和一个低延迟 C++ realtime profile 已在 TensorRT 下实测通过；qiqi profile 首帧出声约 320ms，稳态 pipeline p95 约 93ms。Qwen3.5-0.8B 已量化验证（GPU 36-45 t/s）；预处理、HTTP/UDP 数据面和 state daemon 已打通，PR #1 / #2 已合入 main。当前主要卡点：物理实时 demo 启动不确定（工厂自动挑首个模型）、全长动态 ONNX 在 sm87 回退 CPU 且冷启动内存逼近上限、`.index` 生产预加载停用、PipeWire 物理声卡仍为 stub、文字路未接入守护进程。

---

## 0.5 已完成（2026-08-30 会话）

- [x] **preprocessor 重写为 `mozart-pre` daemon**（删 stage/pipeline 死抽象 + wet/dry 启发式 + 离线 demo）：
  - `src/capture.c` — ALSA 采集：S16_LE / 2ch / 48k / period 960（20ms），overrun 自动恢复+计数。麦克风契约：PCM S16_LE、FL/FR 两路为同信号副本（差 2 采样）→ 只取 FL 避免梳状滤波。**注意：USB 麦尚未插板验证 live 采集（arecord -l 当前只有 APE）**
  - `src/dsp.c` — 取 FL → f32 → 80Hz 高通 → RNNoise **全湿**（干掉 wet/dry SNR 启发式）→ 33-tap Kaiser 3:1 降采样 → 限幅 ±1.0 → 契约帧。VAD 单一来源 = RNNoise 概率；分段滞回：连续 3 帧 ≥0.6 进段 / 25 帧 <0.3 退段（60ms/500ms），`segment_id` 单调递增、静音归 0。`mozart_dsp_reset()` 一键清空全部有状态（为滑窗 segment 重置预留）
  - `src/wav.c` + `-i` 离线模式 — 无麦克风可全链验证
  - 单测 5 组全过（FL 取样/FR 隔离/直通增益/元数据/reset），构建零警告
- [x] **闭环验证**：1000 帧 UDP 包格式正确（1300B / magic 0x4D5A5254 / idx 递增）；rvc-backend 实际收流 200 帧无错误（mock 直通模式）
- [x] **RNNoise 权重出代码**：78MB `rnnoise_data.c` 移除，模型走 `assets/rnnoise_default.rnnb`（14MB blob）运行时加载（USE_WEIGHTS_FILE），librnnoise.a 12MB→242KB；切换前后 PCM SHA256 bit-exact；顺手修上游 fopen NULL 段错误
- [x] **P0 滑窗分块实现（2026-08-30）**：quality/legacy `StreamingRvc` 保留 2s 窗（T=200）和 60ms 交叉淡化；低延迟 profile 另走 240ms block + 2.5s rolling past + SOLA。两条路径均在独立推理线程运行，不连续事件全量重置，静音窗跳推理，块异常降级静音不崩。
- [x] **低延迟 upstream realtime profile（2026-09-05）**：240ms block + 2.5s rolling past + pitch cache + split Generator + SOLA；首帧约 320ms，试听验收合格
- [x] **live 麦克风实测**：HK MIC（ff0f:0001）契约与实测完全一致，mozart-pre live 250 帧无 overrun；板载增益已顶满（15.6dB）
- [x] **产品决策更新**：TTS 从"不做"改为 demo 可选"读出来"开关。`tools/tts_service.py`（Matcha 中文 TTS）已落地，`tools/demo_fullchain.py --speak` 已验证；STT 选型定向 sherpa-onnx 流式（详见 P2）
- [x] **过期二进制排查与重建（2026-09-05 下午会话）**：`rvc-backend/build`（9/1 编）与 `preprocessor/build`（8/30 编）落后于源码，加载 `qiqi-zh-realtime` 报 "Model files missing"。已重建两者；确认顶层 `build-gpu/`（当日 16:1x，晚于最后一次提交）为最新全组件构建，`mozart-pre` / `mozart_stated` / `libmozart_monitor` 均可执行验证通过。踩坑细节见 §3
- [x] **低延迟 realtime UDP 端到端重放验证（2026-09-05）**：新二进制 + `rvc-golden/qiqi-zh-run/backend.yaml`（UDP 18101 / HTTP 18181），`tools/stream_audio_udp.py` 假麦送 18s espeak 中文输入 → 88 块推理，0 inference error / 0 late block / 0 reset / 0 丢包，输出 18.12s@48kHz（mean -21.5dB / peak -0.8dB）；16 个冷启动 underrun 由 `--trim-leading-underruns` 裁掉。realtime 资产（hubert/rmvpe-realtime + split front/decoder）全部 TensorRT 直载
- [x] **FILE_RVC HTTP 全链验证（2026-09-05）**：`/api/mode/switch` → `/api/file/convert` → `/api/file/status?job_id=` → `/api/file/result`，de_narrator 输出 18.1s@48kHz 正常（mean -21.5dB / peak -1.7dB）。注意 `/api/file/status` 不带 `job_id` 时返回 "job not found"

---

## 1. 实测基准（背书数字，勿再重测）

### 1.1 RVC 三模型（TensorRT）

| 模型 | 输入窗口 | FP32 | FP16 | GPU 占用率 |
|---|---|---|---|---|
| RMVPE（F0） | mel 128×128 = **1.28s**，固定 | 20.9 ms | **9.4 ms** | ~0.7% |
| HuBERT（特征） | audio 3200 = **0.2s** | 9.2 ms | **4.5 ms** | ~2.3% |
| Generator（合成） | T=200 = **2s**，固定 | 89.9 ms | **37.6 ms** | ~1.9% |

全链合计 ≈ 98ms / 2s 音频 ≈ **5% GPU**。

### 1.2 LLM（Qwen3.5-0.8B，llama.cpp CUDA，-c 2048 关思考）

| 量化 | 磁盘 | CPU 速度 | GPU 速度（solo） |
|---|---|---|---|
| Q8_0 | 833 MB | 5.4 t/s ❌ | 36.3 t/s |
| Q4_K_M | 542 MB | 5.9 t/s ❌ | 45.1 t/s |

内存峰值（RSS）：Q8 1.7GB / Q4 1.2GB。**必须显式 `-c 2048`**，默认 262144 上下文会把峰值撑到 4.7GB。

### 1.3 并发（RVC + LLM 同板）

| 场景 | RVC Gen FP16 | LLM Q8_0 | LLM Q4_K_M |
|---|---|---|---|
| 最坏情况（RVC 背靠背轰满 GPU） | 38.9ms（+3.3%） | 16.0 t/s | 17.5 t/s |
| 真实节奏（RVC 每 2s 一块，~2% 占空比） | 无感 | **42.6 t/s（无退化）** | — |

内存峰值（最坏并发）：3.35GB / 7.4GB。**结论：算力和内存都管够，不存在调度难题。**

---

## 2. TODO（按优先级）

### P0 — 实时路能出声的前提

- [ ] **物理实时 demo 启动确定化（demo 前置，2026-10-02 审计优先级 1）**：模型工厂按目录顺序自动加载首个可用音色，直接切 `rt_rvc` 不保证选中 `qiqi-zh-realtime`；需要显式初始模型配置或启动器，使用稳定 ALSA 名称而非卡序号。验收：一条命令拉起目标模型 + 后端 + 采集/播放；`/health` 报告实际 split profile 与引擎资产；先用保留的 paced 输入通过，再试听真实麦克风/线缆/输出；分别测量首帧出声、稳态延迟、ALSA overrun 与流计数。
- [ ] **运行时内存上界 + 动态 ONNX GPU 回退（审计优先级 2）**：见 P1。
- [x] **AudioWorker 改滑动窗口分块**（2026-08-30 完成，见 §0.5）：`StreamingRvc` 已落地，剩余为真模型出声后的主观调优。
- [x] **延迟目标改写**：低延迟 upstream realtime profile 的已验收结果为首帧约 320ms、稳态 pipeline p95 约 93ms；quality/legacy 路径仍受 Generator T=200 约束。
- [x] **真模型 C++ realtime 出声验证**：qiqi profile 已完成 TensorRT UDP 端到端验证，0 inference error、0 late block、0 丢包
- [x] **补全扬声器出声路径**（方案 A，2026-09-01）：`mozart-pre -o <alsa设备>` 已实现——同进程 UDP 接收线程收 3860B 输出包 → ALSA 播放（48k f32 mono → S16 立体声复制）。支持 e2e 延迟统计（pts_ns 透传）。已随 qiqi realtime profile 联合验证。

### P1 — GPU 推理落地

- [x] **固定形状 GPU 推理落地**：qiqi realtime 特征和 split Generator 通过 TensorRT 直载，已在 sm87 验收。
  - a) 源码编译 ORT（`--use_cuda --use_tensorrt`，CUDA 13.2 + TRT 10.16 + cuDNN 9.20 齐备）；
  - b) C++ 直接加载 `.engine`（绕过 ORT，已落地 `TrtEngine`，头文件在 `/usr/include/x86_64-linux-gnu` 之外找 NvInfer.h）；
  - c) JetPack 官方 release 的 aarch64 ORT 包是 CPU-only。
- [ ] **普通动态 ONNX 仍回退 CPU（2026-10-02 审计确认）**：全长动态图报 `cudaErrorNoKernelImageForDevice`，ORT 自动重建 CPU session；冷启动可用内存一度降至 ~467 MiB、swap 占用 ~566 MiB。需按目标 sm87 校验 ORT/CUDA 依赖构建，或为每个所需 shape 提供已验证的 TensorRT 资产；并在 `/status` 暴露实际执行后端与回退信息。
- [ ] **有界模型缓存 + 按模式懒加载**：`ModelManager` 当前保留已加载模型，feature 初始化同时加载 quality/realtime 资产，IDLE 不释放引擎。需引入缓存上界与安全生命周期，并在重复模型/模式切换下验证内存有界。
- [x] 跳过 ORT 直接加载 `.engine`：CMake 支持 `USE_TENSORRT=ON`，将 `.engine` 放同名 `.onnx` 旁即可自动加载。

### P2 — 文字路

- [x] **STT 选型已定向：sherpa-onnx 流式**（中文流式 Zipformer，~300MB，CPU 实时 RTF~0.1x，不抢 GPU）。实施顺序：
  - [x] mozart-pre 双发（`-b IP:端口`，发送失败不致命）✅ 2026-08-30
  - [x] stt-service 独立进程收流 + sherpa-onnx 出字（`tools/stt_service.py`，模型 `~/models/sherpa-onnx/zipformer-zh-14M`，54MB，hf-mirror 下载）✅ 实测 2 final 句
  - [x] `segment_id` 驱动断句（段切换出 final；+1s 空闲兜底断句；partial 去重）✅
  - [x] llama-server 常驻 ✅ 2026-08-30：CUDA 版重编（b-9723942，固化回 ~/mozart-archive），Q4_K_M `-c 2048 -ngl 99` @18200，**翻译必须 `chat_template_kwargs:{"enable_thinking":false}`**（思考模式默认开会吃光 max_tokens）
  - [x] 文字路全链胶水 `tools/subtitle_bridge.py` ✅ 实测：STT final → 翻译 0.57-0.70s/句 → 字幕 JSONL + `--speak` TTS 播报（~2s/句）
  - [x] 字幕 SSE 输出壳 ✅ 2026-08-30：后端 `GET /api/subtitles` 已实现 SSE tail（读取外部字幕 JSONL 文件）；前端 Vue 3 已新增 SUB 字幕条组件。
  - [ ] **字幕 SSE 恢复缺口（2026-10-02 审计）**：tail 持有已打开的文件描述符，字幕 JSONL 被替换（新 inode）后不再产生事件；需按 inode/文件轮换重开。
  - [ ] **文字路接入生产守护进程**：当前 STT/翻译/TTS 由外部 Python 工具（`tools/stt_service.py`、`tools/subtitle_bridge.py`）独立运行并写 `/tmp/opencode/subtitles.jsonl`，未由 `mozart_stated` 统一拉起与守护。
  - [x] 前端技术栈升级（2026-08-31）：vanilla DOM → **Vue 3 + Vite SFC**；生产 dist 构建通过（gzip 38KB）
  - [ ] **前端实时面板接线**：`frontend/src/App.vue` 中 `showLive` 恒为 `false`，两个 canvas 波形未接数据；"静音麦克风 / 旁路直通"按钮 ✅ 2026-09-12 已实现（`POST /api/realtime/routing`，静音=输出静音帧不推理、直通=16k 干声升采样直出，状态在 `status.realtime`，跨模式切换保持）；`音色管理库` 按钮未实现。
  - [ ] 并发验证 ASR+LLM+TTS 加入后的共存（当前实测：ASR ~0.3GB CPU + LLM ~1.2GB GPU + TTS 按需，余量足；待与 RVC 三方压测）
- [x] **TTS 小型部署（2026-08-30 实测）**：`tools/tts_service.py`（sherpa-onnx 三引擎）。**Matcha zh-baker 为推荐引擎：RTF ~0.2（5 倍实时，4 线程 CPU），共 90MB**；melo/kokoro int8 也能跑但 RTF 1.6-3 不实时（留存参考）。HDMI 播放（plughw:1,3）已验证。原来"TTS 不做"的决策更新为：**demo 可选"读出来"开关**，句子级延迟完全够
- [x] **TTS 接线 + 全链演示（2026-08-31）**：`tools/demo_fullchain.py` 串起 **语音→ASR→LLM翻译→TTS→RVC变声→HDMI播放** 全链闭环（file 模式稳出声）。实测单句：ASR 0.7s / LLM 0.9s / TTS 13.5s（5s 音频，CPU 挤）/ RVC 27s（CPU RTF≈5）。关键坑：Matcha 纯中文词库读不了英文（换 melo 中英混读）；melo 输出安静 + rms_mix_rate 会把安静包络带进变声输出（发送前峰值归一化 0.9）；后端构建的 RNNoise blob 路径指向 rvc-backend/assets（已拷贝）。`tools/tts2rvc.py` 为 UDP 实时变声通路（P1 GPU 化后启用）
- [ ] **并发基准（2026-08-31 实测，`tools/bench_concurrent.py`）**：极限并发（RVC realtime 流 + TTS + ASR + LLM 同板）尚需用已验收的 qiqi profile 重测；旧数据中的 RVC CPU ONNX 短板不再代表 realtime TensorRT 路径。注意：后端只回包给"首个 UDP 客户端"，多消费者需各开一路或改广播
- [ ] TTS/RVC 提速：继续评估普通 quality/file 路径的 GPU ONNX fallback，并将 TTS 从 CPU 推理迁移到 GPU（如收益明确）
- [ ] **零样本变声（比赛杀招）**：Seed-VC（`github.com/DonkeyHang/seedVC`）——已做调研与 Orin 实测，见 [reports/seedvc-zeroshot-research-20261003/RESEARCH.md](reports/seedvc-zeroshot-research-20261003/RESEARCH.md)。结论：离线可跑（tiny RTF 0.35/4 步、base 0.78/10 步），但峰值内存 5.4–6.1 GB，**必须与 RVC 互斥卸载**；实时档未打通，不建议押注 `RT_ZERO_SHOT`。建议先做 `FILE_ZERO_SHOT`（Python sidecar + 复用 FILE_RVC 外壳）。**待办**：① 明确需求是 VC 还是零样本 TTS；② 许可证 GPL-3.0 评估；③ 若做实时需先把流式分块逐块计时。

### P3 — 收尾

- [x] ~~index 检索链路未验证~~ —— **实为必挂，已重写并验证（2026-09-28）**：原 `index_search.cpp` 读的是凭空构造的布局（`IwFl` 魔数之后字段全部对不上），任何真实 faiss 文件都加载不了。已按 faiss 1.7.2–1.15 IndexIVFFlat 真实序列化布局重写（含 `"full"`/`"sprs"` 倒排表、strict 校验、扁平存储），新增 `test_index_search` 单测：与真实 faiss 生成的 fixture 对照检索结果逐帧一致；并用 31.6MB 真实 RVC index（added_IVF256, ntotal=10000, HF 上游模型）集成验证，12 组随机查询与 faiss nprobe=1 零误差。**剩余**：`de_narrator.index` 本体仍缺失，需训练侧导出后放入 `models/de_narrator/` 板上端到端试听。**生产状态（2026-10-02）**：`RVCModel` 仍停用 `.index` 预加载（每个模型常驻 ~115MB），`index_rate` 默认 0，检索不参与推理；解析器与单测就绪不等于生产检索已启用。
- [x] ~~mel 谱图还是占位实现~~ —— **已实现**：`rvc-backend/src/rvc/feature_extractor.cpp` 已包含 radix-2 FFT + HTK mel 滤波器组 + Slaney 归一化，RMVPE 输入为真实 mel。
- [x] ~~harvest/pm F0 占位~~ —— **已移除（2026-09-28）**：两者从未实现，曾静默返回全零 F0 直接毁掉变声输出。现在仅支持 `rmvpe`：运行时 API/预设传入即拒绝，旧配置在构造时强制回 `rmvpe` 并告警，`test_feature_extractor` 覆盖拒绝路径。
- [ ] 新导出的 `generator_dynamic.onnx` 与原 `de_narrator.onnx` 输出一致性校验（数值对比）后再替换。**脚本已就绪**：`rvc-golden/compare_generator_models.py`（`--self-test` 已本地验证；喂相同输入对比 max/mean 误差、余弦、样本数，`--probe-lengths` 顺带实测动态轴声明），板上跑法见 `rvc-golden/README.md`。**2026-10-02 诊断结论**：两个导出都含 `RandomNormalLike`/`RandomUniform`，相同输入重复推理结果不同，且 T=50/100 失败；因此大幅数值差异不能判定哪个模型正确，也未替换任何资产。下一步：用共享显式噪声（或确定性均值路径）重导，再对比捕获张量并验证后端。
- [x] ~~DESIGN.md 更新~~：§1 延迟指标、§5.4 缺口清单、§4.3 实时性策略（逐帧→分块）均已同步，§2.2/§6 state_manager 状态已从"未编码"改为"已编码"。
- [x] ~~README.md / TODO.md 自身清理~~：同步 TTS 决策、mel 实现、state_manager 落地等不一致描述；README 已改为中文。

---

## 3. 已验证不可行（别再踩）

- ❌ **Generator 动态 T**：RVC 架构级限制（attention reshape 常量折叠写死 T=200），重导出、改 wrapper、ORT/TRT 都试过，只有 T=200 能跑。
- ❌ **CPU 跑 LLM**：5-6 t/s，单句 ~25s，字幕路直接出局；GPU 是唯一路线。
- ❌ **20ms 逐帧跑 HuBERT**：pos_conv 在 T=1 崩溃，最低 ~200ms 窗口。
- ❌ **HuBERT/Generator ONNX 在 TRT 开动态 profile**：generator 任何 min≠opt≠max 组合都报 reshape volume 错，只能 min=opt=max 固定形状。
- ❌ **MTP 投机解码救 CPU**：draft 开销 > 收益（5.4 → 2.2 t/s）。
- ❌ **Q4 在 CPU 上比 Q8 快**：该架构（线性注意力）不成立，两者都是 ~5 t/s。
- ⚠️ **不设 `-c 2048` 跑 llama**：默认 262144 上下文 → 峰值 4.7GB，8GB 板会 OOM。
- ⚠️ **改源码后忘重编**：9/5 低延迟 realtime 代码合入后，旧 `rvc-backend/build` 二进制（9/1 编）加载 `qiqi-zh-realtime` 报 "Model files missing"——旧代码没有 split realtime 资产概念，报错文案有误导性。验证前先核对二进制 mtime 晚于最后一次源码改动；当前可信构建是顶层 `build-gpu/`（ninja 全组件）与重建后的 `rvc-backend/build`、`preprocessor/build`。

---

## 4. 产物索引（已归档至 `~/mozart-archive/`，2026-08-28）

| 产物 | 位置 |
|---|---|
| **归档根目录**（README 含复现命令） | `~/mozart-archive/` |
| 重导出 Generator ONNX（infer 路径） | `rvc-backend/models/de_narrator/generator_dynamic.onnx` |
| TensorRT FP16 引擎（Gen T=200） | `rvc-backend/models/de_narrator/gen_v2_fp16.engine` |
| 全部 TRT 引擎（7 个有效）+ 压测日志 | `~/mozart-archive/rvc-trt/` |
| Qwen3.5-0.8B 纯文本 GGUF（vision 已剥离） | `~/mozart-archive/qwen35/gguf/`（f16 / Q8_0 / Q4_K_M） |
| ModelScope 原始下载（重导 GGUF 用） | `~/mozart-archive/qwen35/hf-orig/` |
| llama-cli 运行时（CUDA sm_87 / CPU） | `~/mozart-archive/qwen35/llama.cpp/build-*-bin/` |
| RVC 源码 + 最小化导出脚本 | `~/mozart-archive/RVC/export_gen_minimal.py` |
| .pth 音色权重（4 个） | `~/models/` |

> `/tmp/opencode/` 原件可删；下次实验若需重建 llama.cpp/TRT 引擎，归档 README 里有完整编译/构建命令。

---

## 5. USB 声卡形态（2026-09-06 已定案，见 usb-gadget/DECISION.md）

**结论**：Tegra234 XUDC device mode 不支持 ISO 端点（NVIDIA 文档明示 + 实测
ISO 流必现 ring underrun 0xe，软件修复候选全部无效），标准 UAC1/UAC2 USB
声卡在本机不可实现。gadget 路线锁死关闭，系统已恢复官方状态
（`usb-gadget/RESTORE-20260906.md`）。

- [x] 根因调查与证据归档：`usb-gadget/candidate/`、`iso-test-20260906/`、
  `completion-test-20260906/`、`crash-20260905-serial-test/`
- [x] 系统恢复：initrd / tegra-xudc.ko / 服务全部还原为官方原件
- [x] **最终方案**：USB→3.5mm 声卡小尾巴插 Jetson USB-A host 口
  （免驱 UAC），3.5mm 线进电脑麦克风口；`mozart-pre -o plughw:<小尾巴>`
  联调，板端零内核改动
- [ ] 采购对录线/小尾巴（验收标准见 `usb-gadget/DECISION.md`）
- [ ] `mozart-pre -o plughw:<小尾巴>` 端到端联调（线到货后 10 分钟级）
