# Seed-VC 零样本变声调研（Jetson Orin Nano Super 8GB）

日期：2026-10-03
状态：调研 + 板上实测（离线路径）；实时路径未打通
上游：`github.com/DonkeyHang/seedVC` @ `51383ef`（本地检出 `~/mozart-archive/seedVC`）
许可证：**GPL-3.0**（见下"风险"）

---

## 1. 结论先行

1. **零样本变声在 Orin Nano 8GB 上"能跑，但很重"**。Seed-VC 离线转换已在本板实测成功，
   但峰值内存 5.4–6.1 GB，**必须与 RVC 推理互斥卸载**，不能共存。
2. **实时档（RT_ZERO_SHOT）暂不建议押注**。最轻的 tiny 档离线 RTF 0.35（4 步），
   按上游在 RTX 3060 Laptop 的每块 150 ms 参考推算，Orin 上单块很可能超出实时预算；
   按上游实时参数（2.5 s 左上下文 + 3 s prompt）在本板直接内存崩溃。
3. **建议先做 `FILE_ZERO_SHOT`**（上传参考音 + 源音 → 克隆输出），作为比赛"杀招"的可控形态；
   `RT_ZERO_SHOT` 留到流式路径实测达标后再开。
4. **不要在 C++/ONNX 里重写整个 Seed-VC**。它由 Whisper/XLSR 编码器 + flow-matching DiT
   迭代采样 + BigVGAN/HiFT 声码器组成，迭代采样循环不适合单张 ONNX。建议先作为
   Python sidecar 接入（与现有 STT/LLM/TTS 同构），复用 `FILE_RVC` 的 job/API 外壳。
5. **许可证是硬风险**：Seed-VC 为 GPL-3.0。若产品需要闭源分发，需评估替代
   （kNN-VC、自研 ONNX 路径）或走"仅 demo 使用、不随产品分发权重/代码"的路线。

---

## 2. 候选方案对比

| 方案 | 类型 | 实时 | 说明 | 与本项目的契合度 |
|------|------|------|------|------------------|
| **Seed-VC v1 tiny** | 语音转换 VC | ✅（上游有流式） | 25M DiT + XLSR-300m + HiFT，22.05 kHz | 最贴合；本报告实测对象 |
| Seed-VC v1 whisper-small | VC | ❌ | 98M DiT + Whisper-small + BigVGAN | 质量更好、更重，适合 FILE |
| Seed-VC v2 AR+CFM | 语音+口音转换 | 部分 | 67M CFM + 90M AR，抑制源音色更强 | 备选，更重 |
| OpenVoice | 音色转换 | ❌ | 需基底 TTS 先出音，再转音色 | 不是纯 VC，适合"翻译后朗读" |
| CosyVoice / XTTS | 零样本 TTS | ❌ | 文本 → 指定音色语音 | 若目标只是"让翻译读出来"更直接 |
| kNN-VC | VC | 部分 | WavLM + HiFiGAN kNN，结构简单、确定性强 | ONNX 化相对容易的备选 |
| RVC 本身 | 非零样本 | — | 每个音色需训练 | 不满足"任意参考音即克隆" |

> 说明：比赛文案是"翻译到目标语言后零样本克隆指定音色"。如果诉求是**把译文用指定音色读出来**，
> 零样本 TTS（CosyVoice/XTTS）比 VC 更直接；如果诉求是**保留原说话内容与语气、只换音色**，
> 则 Seed-VC / kNN-VC 这类 VC 更合适。建议先明确这一点再选型。

---

## 3. Seed-VC 规格（实测/缓存核对）

| 资产 | 大小 | 用途 |
|------|------|------|
| `DiT_uvit_tat_xlsr_ema.pth`（tiny） | 136 MB | 实时档 DiT |
| `DiT_seed_v2_uvit_whisper_small_wavenet_bigvgan_pruned.pth` | 420 MB | 质量档 DiT |
| `facebook/wav2vec2-xls-r-300m` | ~1.2 GB | tiny 档内容编码器 |
| `openai/whisper-small` | ~1 GB | whisper 档内容编码器 |
| BigVGAN 22k / HiFT | 数百 MB | 声码器 |
| CAMPPlus | 27 MB | 音色风格编码 |

- 采样率：22.05 kHz；输出时长与输入对齐（本报告实测 10.00 s 输入 → 10.00 s 输出）。
- 上游实时参考（RTX 3060 Laptop）：tiny + 10 步 + block 0.18 s，单块 150 ms，算法延迟约 430 ms。
- 上游实时可运行判据：**单块推理时间 < block time**。

---

## 4. 板上实测（2026-10-03）

环境：Jetson Orin Nano Super 8GB（MAXN），`~/vc_backend_venv`（torch 2.12.0+cu132），
fp16，源音 `glados_0.wav`（10.0 s），参考音 `s1p1.wav`（14.6 s），cfg 0.7。

| 档位 | 扩散步数 | 离线 RTF | 峰值 used | 峰值 swap | 备注 |
|------|---------|---------|-----------|-----------|------|
| base（whisper-small + BigVGAN） | 10 | **0.78** | 6132 MiB | 718 MiB | 载入警告：`input_pos` / `f0_embedder.weight` 形状不匹配 |
| tiny（XLSR + HiFT） | 4 | **0.35** | 5370 MiB | 715 MiB | 输出 10.00 s @22050，RMS −14.4 dB，峰值 −0.1 dB |
| tiny + 上游实时参数（block 0.5 s / 左 2.5 s / prompt 3 s / 10 步） | 10 | 崩溃 | 6861 MiB | 840 MiB | `NVML_SUCCESS … INTERNAL ASSERT`（内存压力） |

RTF = 生成耗时 / 音频时长，由上游 `inference.py` 打印。日志与输出音频见本目录。

**解读**：
- 离线 tiny 约 2.9× 实时；base 约 1.3× 实时。单看吞吐，离线批量够用。
- 峰值内存 5.4–6.1 GB 意味着**必须独占**：RVC 常驻约 2 GB，两者同时在场会 OOM
  （与 DESIGN §6.2 的"跨大类卸载置换"设计一致）。
- 流式实时档在合理上下文下逼近/超过 8GB 上限，且本报告的 headless 流式 harness 还撞上了
  上游旋转位置缓存与长度公式的问题（见日志），**未能得到可信的每块延迟数字**。
  要判断 RT 可行性，必须先把 `real-time-gui.py` 的 `custom_infer` 分块逻辑在板上跑通并逐块计时。

---

## 5. 集成方案（建议）

分两步，先离线后实时：

### 5.1 FILE_ZERO_SHOT（建议先做）

```
POST /api/zero-shot/convert  (reference_wav + source_wav)
  → 复用 FILE_RVC 的 job 队列/存储/结果下载外壳
  → 调用 Seed-VC Python sidecar（与 STT/LLM/TTS 同构）
  → 返回 48k/22k WAV
```

- 新增 `python tools/seedvc_service.py`，常驻加载 tiny（或 base）模型，HTTP/管道接收 job。
- `mode_controller` 已有的 `file_zero_shot` 槽位与 501 分支改为路由到该 sidecar。
- 内存：进入 zero-shot 前必须卸载 RVC 引擎（跨大类置换），退出后按需重载。

### 5.2 RT_ZERO_SHOT（条件式）

- 只有当板上逐块实测 `单块推理 < block time` 且总内存 < 6GB 时才推进。
- 需要把 `real-time-gui.py` 的分块/SOLA 逻辑搬到可 headless 的 runner，再决定
  block/上下文/步数，并对比延迟与音质。

### 5.3 不建议的路线

- **不要**试图把整个 flow-matching 采样循环导成单张 ONNX/TRT。可导出的只是编码器/声码器，
  迭代循环仍需宿主代码；前期收益不抵成本。
- 参考音管理需要新概念：现有模型按 `<id>.onnx` 一音色一模型，零样本改为"参考音频 + 提示"，
  需要新增参考音上传/缓存与相似度预览。

---

## 6. 风险

| 风险 | 说明 | 缓解 |
|------|------|------|
| **许可证 GPL-3.0** | 强 copyleft，闭源产品集成需谨慎 | demo 自用可，产品化前评估或换 kNN-VC/自研 |
| 内存 | 峰值 5.4–6.1 GB，独占 RVC | 严格模式互斥 + 卸载置换 |
| 实时性不确定 | 流式路径未实测；上游参考来自 3060 | 先离线；RT 以板上逐块实测为准 |
| 载入警告 | `input_pos` 等形状不匹配，可能影响音质 | 用官方 config/ckpt 配对，做 Golden 式对齐 |
| 推理栈不一致 | Seed-VC 是 PyTorch，Mozart 是 ONNX/TRT 纯 C++ | 先 sidecar，长期再评估导出 |
| venv 污染 | 本次为跑通装了 `descript-audio-codec`、`python-dotenv`；临时改的 torchaudio 版本检查已还原 | 记录在此，避免影响 RVC golden |

> 关于麦克风：现场麦克风采集路径此前已在板上验证（用户 2026-10-03 确认）。
> 本调研只针对零样本变声，不重复采集测试。

---

## 7. 复现命令

```bash
# 依赖（一次性；会改动 vc_backend_venv）
~/vc_backend_venv/bin/pip install descript-audio-codec==1.0.0 python-dotenv

# 离线：base 档
cd ~/mozart-archive/seedVC
python /tmp/seedvc-test/run_seedvc.py \
  --source examples/source/glados_0.wav \
  --target examples/reference/s1p1.wav \
  --output /tmp/seedvc-test --diffusion-steps 10 --fp16 True

# 离线：tiny 实时档（需 tiny ckpt/config，见 reports 目录命令历史）
python /tmp/seedvc-test/run_seedvc.py \
  --source examples/source/glados_0.wav \
  --target examples/reference/s1p1.wav \
  --output /tmp/seedvc-tiny --diffusion-steps 4 \
  --checkpoint <DiT_uvit_tat_xlsr_ema.pth> \
  --config <config_dit_mel_seed_uvit_xlsr_tiny.yml> --fp16 True
```

> 注：`run_seedvc.py` 是本次调研的临时 wrapper，把 `torchaudio.save` 换成 soundfile，
> 以绕开该 venv 中 torch/torchaudio 的 CUDA 版本检查（临时补丁已还原）。若后续长期使用
> Seed-VC，建议单独建 venv 或修好 torchaudio。

---

## 8. 待办

- [ ] 明确需求：要"换音色保留内容"（VC）还是"用指定音色朗读译文"（零样本 TTS）。
- [ ] 若做 VC：搭 `tools/seedvc_service.py`，打通 FILE_ZERO_SHOT 端到端并做 Golden 式数值/听感对比。
- [ ] 若要做 RT：把流式分块逻辑 headless 化，逐块实测 `Orin` 上的 block 预算，再决定参数。
- [ ] 许可证评估（GPL-3.0 对产品分发的影响）。
- [ ] 若 ONNX 化是硬需求，优先评估 kNN-VC 作为可导出备选。
