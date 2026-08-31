# TODO

> 2026-08-28 板上实测后落盘，2026-08-30 预处理重写后更新。所有数字均在 Jetson Orin Nano Super 8GB（MAXN_SUPER 满频）实测，非估算。
> 配套文档：TARGET.md（定位）、DESIGN.md（设计）。本文件只管"接下来做什么"。

---

## 0. 一句话现状

RVC 三模型已全部在 TensorRT 下实测通过（FP16 加速 2.4×，全链 GPU 占用 ~5%）；Qwen3.5-0.8B 已量化验证（GPU 36-45 t/s）；**预处理已于 2026-08-30 重写完成并打通 rvc-backend 闭环**（RNNoise 全湿 + FL 取样 + 3:1 降采样 + VAD 滞回分段 + MZRT UDP 发送）；产品决策：TTS 不做（文字路终点是字幕）。卡脖子的架构问题不变：RVC 模型只吃大块音频（0.2s / 1.28s / 2s），AudioWorker 逐帧（20ms）喂数链路必须改成滑动窗口分块；此外全链还没有真实的扬声器出声路径。

---

## 0.5 已完成（2026-08-30 会话）

- [x] **preprocessor 重写为 `mozart-pre` daemon**（删 stage/pipeline 死抽象 + wet/dry 启发式 + 离线 demo）：
  - `src/capture.c` — ALSA 采集：S16_LE / 2ch / 48k / period 960（20ms），overrun 自动恢复+计数。麦克风契约：PCM S16_LE、FL/FR 两路为同信号副本（差 2 采样）→ 只取 FL 避免梳状滤波。**注意：USB 麦尚未插板验证 live 采集（arecord -l 当前只有 APE）**
  - `src/dsp.c` — 取 FL → f32 → 80Hz 高通 → RNNoise **全湿**（干掉 wet/dry SNR 启发式）→ 33-tap Kaiser 3:1 降采样 → 限幅 ±1.0 → 契约帧。VAD 单一来源 = RNNoise 概率；分段滞回：连续 3 帧 ≥0.6 进段 / 25 帧 <0.3 退段（60ms/500ms），`segment_id` 单调递增、静音归 0。`mozart_dsp_reset()` 一键清空全部有状态（为滑窗 segment 重置预留）
  - `src/wav.c` + `-i` 离线模式 — 无麦克风可全链验证
  - 单测 5 组全过（FL 取样/FR 隔离/直通增益/元数据/reset），构建零警告
- [x] **闭环验证**：1000 帧 UDP 包格式正确（1300B / magic 0x4D5A5254 / idx 递增）；rvc-backend 实际收流 200 帧无错误（mock 直通模式）
- [x] **RNNoise 权重出代码**：78MB `rnnoise_data.c` 移除，模型走 `assets/rnnoise_default.rnnb`（14MB blob）运行时加载（USE_WEIGHTS_FILE），librnnoise.a 12MB→242KB；切换前后 PCM SHA256 bit-exact；顺手修上游 fopen NULL 段错误
- [x] **P0 滑窗分块（2026-08-30）**：`StreamingRvc` 落地——2s 窗（T=200 死约束）→ 独立推理线程 → 最小 60ms 交叉淡化拼接 → 输出环。稳态延迟 ≈ 2s + 推理（50% hann OLA 因 +1s 尾巴被否决）。hop=31040（97 帧整，避免帧量化漂移）；不连续（idx 缺口/segment 变化/pts 跳变）全量重置；静音窗跳推理；块异常降级静音不崩。单测逐样本连续性对拍通过；后端冒烟 400 帧→恰好 4 窗调度。AudioWorker 双模式：mock 保持 1:1 帧路零附加延迟
- [x] **live 麦克风实测**：HK MIC（ff0f:0001）契约与实测完全一致，mozart-pre live 250 帧无 overrun；板载增益已顶满（15.6dB）
- [x] **产品决策**：TTS 不做（翻译终点是字幕）；STT 选型定向 sherpa-onnx 流式（详见 P2）

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

- [x] **AudioWorker 改滑动窗口分块**（2026-08-30 完成，见 §0.5）：StreamingRvc 落地，剩余为真模型实测调优。
  - 残留调优点：块边界音质需真模型出声后主观评估（60ms 淡化是否足够）；冷启动首窗前输出静音（可改直通透传但会有跳变）；switch_model 与推理线程竞态仍无锁保护
- [x] **延迟目标改写**：新目标 = 端到端 2s + 推理 + 60ms（Generator T=200 架构死约束，30ms/500ms 均不可达；DESIGN.md §1 待同步）
- [ ] **真模型出声验证**：放好 HuBERT/RMVPE/Generator 模型 → mozart-pre live → 滑窗全链 → 主观听感 + /api/status 延迟数字
- [ ] **补全扬声器出声路径**（方案 A）：mozart-pre 加"收回包"——同进程收 3860B 输出包 → ALSA 播放（48k mono float→S16 立体声复制）。不依赖滑窗，配 mock 即可先验证时钟；回包 pts_ns 透传可量真实端到端延迟。备选：独立 player 工具 / PipeWire 直连（demo 前不碰）

### P1 — GPU 推理落地

- [ ] **解决 GPU 版 ONNX Runtime**（三选一）：
  - a) 源码编译 ORT（`--use_cuda --use_tensorrt`，CUDA 13.2 + TRT 10.16 + cuDNN 9.20 齐备）；
  - b) C++ 直接加载 `.engine`（绕过 ORT，需写 TRT runtime wrapper，头文件在 `/usr/include/x86_64-linux-gnu` 之外找 NvInfer.h）；
  - c) 等 JetPack 提供 aarch64 GPU ORT（官方 release 的 aarch64 包是 CPU-only）。
- [ ] 装好后打开 `rvc-backend` 的 `USE_CUDA_EP=ON` 编译开关（`onnx_engine.cpp` 已留好挂载点，默认关闭）。
- [ ] 或跳过 ORT：把 `gen_v2_fp16.engine` 直接接到 C++（引擎已实测，`/tmp/opencode/rvc-trt/`）。

### P2 — 文字路

- [x] **STT 选型已定向：sherpa-onnx 流式**（中文流式 Zipformer，~300MB，CPU 实时 RTF~0.1x，不抢 GPU）。实施顺序：
  - [x] mozart-pre 双发（`-b IP:端口`，发送失败不致命）✅ 2026-08-30
  - [x] stt-service 独立进程收流 + sherpa-onnx 出字（`tools/stt_service.py`，模型 `~/models/sherpa-onnx/zipformer-zh-14M`，54MB，hf-mirror 下载）✅ 实测 2 final 句
  - [x] `segment_id` 驱动断句（段切换出 final；+1s 空闲兜底断句；partial 去重）✅
  - [x] llama-server 常驻 ✅ 2026-08-30：CUDA 版重编（b-9723942，固化回 ~/mozart-archive），Q4_K_M `-c 2048 -ngl 99` @18200，**翻译必须 `chat_template_kwargs:{"enable_thinking":false}`**（思考模式默认开会吃光 max_tokens）
  - [x] 文字路全链胶水 `tools/subtitle_bridge.py` ✅ 实测：STT final → 翻译 0.57-0.70s/句 → 字幕 JSONL + `--speak` TTS 播报（~2s/句）
  - [ ] 字幕输出端（WebSocket → 前端；subtitle_bridge 已产 JSONL，缺前端订阅）；流式 ASR 无标点 → 可用译文标点整句替换
  - [ ] 并发验证 ASR+LLM+TTS 加入后的共存（当前实测：ASR ~0.3GB CPU + LLM ~1.2GB GPU + TTS 按需，余量足；待与 RVC 三方压测）
- [x] **TTS 小型部署（2026-08-30 实测）**：`tools/tts_service.py`（sherpa-onnx 三引擎）。**Matcha zh-baker 为推荐引擎：RTF ~0.2（5 倍实时，4 线程 CPU），共 90MB**；melo/kokoro int8 也能跑但 RTF 1.6-3 不实时（留存参考）。HDMI 播放（plughw:1,3）已验证。原来"TTS 不做"的决策更新为：**demo 可选"读出来"开关**，句子级延迟完全够
- [ ] TTS 接线：stt-service `--json` final 句 → TTS 队列 → aplay 播放（~50 行胶水）；若要统一音色可把 TTS 输出灌 RVC 链

### P3 — 收尾

- [ ] `de_narrator.index` 缺失（DESIGN 约定 `<id>.index`），index 检索链路未验证。
- [ ] mel 谱图还是占位实现（DESIGN §5.4），RMVPE 喂的是假 mel —— 换真 FFT+mel 滤波器组。
- [ ] 新导出的 `generator_dynamic.onnx` 与原 `de_narrator.onnx` 输出一致性校验（数值对比）后再替换。
- [ ] DESIGN.md 更新：§1 延迟指标、§5.4 缺口清单、§4.3 实时性策略（逐帧→分块）。
- [ ] 资产归档：`/tmp/opencode/` 重启会清，固化 GGUF/引擎/导出脚本到仓库或 ~/models。

---

## 3. 已验证不可行（别再踩）

- ❌ **Generator 动态 T**：RVC 架构级限制（attention reshape 常量折叠写死 T=200），重导出、改 wrapper、ORT/TRT 都试过，只有 T=200 能跑。
- ❌ **CPU 跑 LLM**：5-6 t/s，单句 ~25s，字幕路直接出局；GPU 是唯一路线。
- ❌ **20ms 逐帧跑 HuBERT**：pos_conv 在 T=1 崩溃，最低 ~200ms 窗口。
- ❌ **HuBERT/Generator ONNX 在 TRT 开动态 profile**：generator 任何 min≠opt≠max 组合都报 reshape volume 错，只能 min=opt=max 固定形状。
- ❌ **MTP 投机解码救 CPU**：draft 开销 > 收益（5.4 → 2.2 t/s）。
- ❌ **Q4 在 CPU 上比 Q8 快**：该架构（线性注意力）不成立，两者都是 ~5 t/s。
- ⚠️ **不设 `-c 2048` 跑 llama**：默认 262144 上下文 → 峰值 4.7GB，8GB 板会 OOM。

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
