# Golden 与流式 Backend 调查日志

最后更新：2026-09-05

## 目标

使用同一份可复现输入，先固定 PyTorch Golden Model，再逐层对比 Mozart
流式 backend，最后确认 GPU 推理和实时性能满足要求。

## 已锁定的 Golden 标准

黄金输出文件：

`rvc-golden/output/qiqi-zh-espeak-pinyin-mixed-python-reference.wav`

输入文件：

`rvc-golden/input/qiqi-espeak-pinyin-zh-en-mixed.wav`

模型：

`/home/moyamryia/mozart-archive/RVC_Model_Collection/48k（新版）/原神/中文/七七/七七.pth`

当前锁定内容：

- 输出 SHA-256：`01a114c3db4a1175a41ddde86b71318de7e385eef745d6aced4e397f41425dce`
- 输入 SHA-256：`e93d38e7e44a86bbb88f19f073ce21f791bfc2f959e6257554d853fe87d99c51`
- 模型 SHA-256：`d72bd33911b8a9d2ccac81dc005998a2c043e97587374cecf3ca38edef05d64d`
- Golden runner SHA-256：`1fa23fc417a0b7806fc0134e0de61b197f84a8725c62ce901d6d3ac4480b36d5`
- Streaming runner SHA-256：`10c6c58c98c0c7380381e003daf764d04d1d403a2fe69ba492026bac2a64087d`
- RVC 源码 commit：`81eed5e8f68b6bed1789f682fe78cdd324495afc`
- RVC `infer/` 工作树：已确认没有 tracked 修改
- HuBERT、RMVPE 权重和配置：已写入 manifest 并校验

校验命令：

```bash
/home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/verify_golden.py
```

最近一次校验结果：`Golden verification PASSED`。

注意：manifest、校验器和黄金 WAV 当前仍需要由 Git 提交后，才会成为仓库
历史中的不可变锁。本日志只记录工作区状态，不代表已经创建 commit。

## Golden 推理修复

提交 `70ca280` 曾将 Golden 的原始 `generator.infer(...)` 替换为确定性的
均值路径，并将正弦源噪声设置为 `0.0`。这改变了参考模型的合成语义。

已恢复的默认行为：

- 使用原始 RVC `generator.infer(...)`
- 保留固定随机种子 `114514`，保证重复运行结果一致
- 保留原始正弦源噪声
- 确定性 Generator 路径只在显式传入 `--deterministic-generator` 时启用

验证结果：修复后的直接文件输出与此前较好的黄金 WAV 逐样本完全一致。

## 流式 Golden 基线

流式参考脚本：`rvc-golden/run_streaming_reference.py`

固定契约：

- 输入：16 kHz、mono、float32、320 samples / 20 ms
- 窗口：32000 samples / 2 s
- 输入 hop：31040 samples / 97 帧
- 输出交叉淡化：2880 samples / 60 ms
- 每个 block 发送：93120 samples / 1940 ms
- Generator 输出：96000 samples
- flush：2 s

本次 pinyin 中英混合输入生成：

- 流式窗口：12
- 完整输出：23.82 s，包含 2 s 冷启动时间线
- 试听输出：21.82 s，去掉前 2 s 冷启动静音
- 输出样本：全部有限，无 NaN/Inf

逐窗口 Golden 张量保存在：

`rvc-golden/tensors/qiqi_zh_pinyin_mixed_streaming/block_000..011/`

## CPU Backend 实验

backend 使用隔离配置：

`rvc-golden/qiqi-zh-run/backend.yaml`

配置端口：HTTP `18181`，UDP `18101`。

已显式切换模型：`qiqi-zh`。

实际加载日志确认：

- Generator：`qiqi-zh.onnx`
- HuBERT：`hubert_base_dynamic.onnx`
- RMVPE：`rmvpe_dynamic.onnx`
- Runtime：ONNX Runtime CPU
- 没有邻近 TensorRT `.engine`

CPU 单窗口真实推理约 `8.6 s`，但输入每 `20 ms` 持续到达，因此本次 UDP
整段结果无效，不用于音质结论。

状态指标：

- `blocks=4`
- `late_blocks=3`
- `input_overruns=253120`
- `output_underruns=997`
- `inference_errors=0`
- UDP 收包：`1191/1191`，无网络丢帧

结论：主要问题是 CPU 推理无法满足实时节奏，导致窗口被覆盖、输出欠载和
流式时间线错位，而不是已经证明 Generator 音质错误。

## 第一轮数值对比

对 Golden `block_000/input_window_16k.npy` 执行了 C++ `diag_f32`，使用同一
组 HuBERT/RMVPE ONNX 文件，比较结果如下：

- C++ RMVPE 输出帧数：201
- Golden RMVPE 输出帧数：401
- C++ mel 输出：`201 x 128`
- Golden mel 输出：`401 x 128`
- mel 相关系数：约 `0.166`
- F0 相关系数：约 `0.209`

这个比较尚未形成有效结论，因为当前 Golden 流式 block 的 RMVPE mel 捕获
包含的时间轴与 C++ `diag_f32` 使用的窗口规格不一致，下一步必须先确认
Golden mel capture 的实际 shape、`Pipeline` 输入和 padding，再进行严格
的同 shape 对比。不能仅凭当前数字修改 C++ RMVPE。

## GPU / ONNX Runtime 调查

机器硬件和系统库：

- GPU：NVIDIA Orin
- Driver：595.78
- CUDA：13.2
- TensorRT：10.16.2
- `trtexec`：`/usr/bin/trtexec`

原始 `build/` 配置：

```text
USE_ONNX=ON
USE_CUDA_EP=OFF
USE_TENSORRT=ON
```

所以原始 backend 明确使用 CPU。

已创建独立 GPU 构建目录：`build-gpu/`

GPU 构建配置：

```text
USE_ONNX=ON
USE_CUDA_EP=ON
USE_TENSORRT=ON
```

为适配当前 ORT 头文件，已将 CUDA provider 调用改为新版 C++ API：
`SessionOptions::AppendExecutionProvider_CUDA(...)`。GPU binary 已成功编译。

GPU binary 启动时失败原因：

```text
Failed to load library libonnxruntime_providers_cuda.so
```

系统现有：

- `/usr/local/lib/libonnxruntime.so`
- `/usr/local/lib/libonnxruntime_providers_shared.so`

系统缺少：

- `libonnxruntime_providers_cuda.so`
- `libonnxruntime_providers_tensorrt.so`

因此当前不能把 GPU binary 当作已经完成的 GPU backend。GPU 驱动本身正常，
阻塞点是 ONNX Runtime provider 动态库安装不完整。

## TensorRT 调查

qiqi、HuBERT、RMVPE 目录当前没有现成 `.engine`，backend 不会自动进入
TensorRT。

已用 `trtexec --skipInference` 检查动态 ONNX：

- qiqi Generator：可开始 TensorRT 解析和构建，但完整 profiling 时间较长，
  本次未完成 engine 生成
- HuBERT dynamic：因未提供动态 optimization profile，自动使用错误 shape，
  构建失败
- RMVPE dynamic：因未提供动态 optimization profile，构建失败

下一次 TensorRT 构建必须显式指定与 backend 契约匹配的 profile。特别是：

- HuBERT 输入必须支持 `[1, 32000]`
- RMVPE 输入必须支持 `[1, 128, T]`，并保持 mel bin/time 布局
- Generator 当前流式契约固定为 `T=200`

## Provider 依赖补齐（2026-09-01）

缺失的 CUDA/TensorRT provider 已在**不改动系统软件**的前提下补齐：

- 系统 `/usr/local` 的 ORT 1.19.2（CPU-only）保持原样，未删除、未覆盖。
- 从 PyPI（NJU 镜像）提取 `onnxruntime_gpu 1.29.0` aarch64 wheel
  （CUDA 13 构建：`libcublas.so.13`/`libcudart.so.13`，与本机 CUDA 13.2 匹配，
  cuDNN 9 / glibc 2.39 均满足）到项目本地
  `third_party/onnxruntime-gpu-1.29.0/{lib,pylib}`。
- 头文件取自官方 GitHub release `onnxruntime-linux-aarch64-1.29.0.tgz`
  （经 gh-proxy 下载）放入同目录 `include/`。
- 系统 ORT 带版本脚本（`VERS_1.19.2`），仅换 `LD_LIBRARY_PATH` 会因符号版本
  不匹配失败，因此已用本地 1.29 头/库重新配置并增量编译 `build-gpu/`
  （`ONNX_RUNTIME_INCLUDE_DIR` / `ONNX_RUNTIME_LIB` 指向 third_party）。
- 启动方式（不装系统、不动 venv）：

  ```bash
  LD_LIBRARY_PATH=third_party/onnxruntime-gpu-1.29.0/lib \
    ./build-gpu/rvc-backend/rvc_backend rvc-golden/qiqi-zh-run/backend.yaml
  ```

- 验证结果：Generator/HuBERT/RMVPE 三个模型日志均出现
  `CUDA execution provider attached`，且 ORT 产生 CUDA 图分区 warning
  （证明 EP 真实生效），HTTP 18181 / UDP 18101 正常监听。
  Python 侧用 `PYTHONPATH=third_party/onnxruntime-gpu-1.29.0/pylib`
  （配 `.venv` 的 cp312）确认 `CUDAExecutionProvider` 可用并成功建会话。

已知缺口：该 aarch64 wheel **不含** `libonnxruntime_providers_tensorrt.so`，
TensorRT EP 仍不可用，只能走 CUDA EP；如需 TRT engine 需另行构建。
Golden 捕获的 per-window `generator_00_pitchf`（398 帧）与
`qiqi-zh-full.onnx` 期望的 2380 帧不一致，下一步对比前必须先理清
full-model 与 backend 窗口契约的对应关系。

## 当前代码与测试状态

已通过：

```bash
ctest --test-dir build/rvc-backend --output-on-failure
```

结果：4/4 tests passed。

GPU 构建新增的 backend 代码变更：

`rvc-backend/src/rvc/onnx_engine.cpp`

当前改动只解决 CUDA EP API 兼容性和编译问题，尚未解决 provider 动态库
缺失问题。

## 下一步

1. ~~补齐 CUDA-enabled ONNX Runtime provider~~（已完成，CUDA EP 可用；
   TensorRT EP 因 aarch64 wheel 未提供而仍缺失，或需自行构建）。
2. 启动后必须从日志确认实际 provider 为 CUDA/TensorRT，拒绝 CPU fallback
   （CUDA EP 已确认；仍需实测推理耗时是否满足实时）。
3. 在无实时压力的单窗口路径比较 Golden 与 backend：窗口输入、RMVPE mel、
   RMVPE F0、HuBERT features、Generator feeds、Generator raw output。
4. 修正 Golden/C++ RMVPE shape 和 padding 对比工具后，再判断 RMVPE 是否有
   数值分歧。
5. 只有 backend 满足 `late_blocks=0`、`input_overruns=0`、
   `output_underruns=0`、`inference_errors=0` 且 UDP 无丢帧时，才生成可听的
   backend 流式参考结果。

## 精度对齐修复（2026-09-02 续）

### 张量级 bisect 结论

按 AGENTS 流程逐层对拍 golden `block_000` 张量：

- **RMVPE 前端逐位对齐**：喂同一 reflect-padded 64000 输入，C++ mel
  rel_rmse ≈ 2%（max_abs 出现在个别 log-mel 下限 bin），F0 voiced 帧中位差
  **0.2 音分**，voiced/nonzero 帧数 276 vs 275 基本一致。RMVPE 不是分歧源。
- **framing 才是分歧源**：golden `run_streaming_reference.py` 把整个 32000
  窗口交给 RVC `pipeline.vc`，后者先 `np.pad(t_pad=16000)`→64000 再喂
  HuBERT；HuBERT 64000→199 帧，`F.interpolate(scale_factor=2)`→398，
  generator **T=398**，输出 191040，再按 `[t_pad_tgt:-t_pad_tgt]`（各去
  48000）裁到 95040。而旧 backend 流式路径把 HuBERT 只喂中心 32000→99 帧、
  generator 用固定 **T=200**→96000，两者帧数与上下文完全不同。

### 生产 backend 的修复（非测试专用）

改的是流式/文件主路径，真实部署同样生效：

- `inferencer.cpp` 流式路径（`audio<=kWindow16k`）与 file windowed 循环统一
  改为 golden-aligned：HuBERT 输入 `reflect_pad(window, kPad16k)`=64000，
  pitch/pitchf 取 `pitch_full[0:T]`（offset 0，非旧的 frame_start），
  generator 按引擎原生 T（398）运行，输出去两侧 `t_pad_tgt` 再补零到
  `kWindow16k*model_sr/16000` 的 block 契约。
- `feature_extractor.cpp` HuBERT TRT 形状校验由固定 `need=32000` 放宽为
  `[1,N>0]`；非契约长度仍走动态 ONNX 兜底。
- 用 `tools/export_generator_onnx.py --frames 398` 重新导出确定性 mean-path
  generator ONNX（自带 PyTorch/ONNX 校验 relative_rmse 0.0007%、corr 1.0），
  `trtexec --noTF32` 构建 `qiqi-zh-t398.engine`；HuBERT 构建
  `hubert_base_64k.engine`。模型目录用符号链接把 `qiqi-zh/qiqi-zh.engine`、
  `qiqi-zh/qiqi-zh.onnx` 指向 T398，`hubert_base_dynamic.engine` 指向 64k，
  不改动 backend.yaml。

### 修复后实测

- 流式：12/12 blocks、inference_errors=0、late_blocks=0，稳态 ~645ms
  （T398 generator 约为 T200 两倍工作量，仍远低于 1940ms 实时预算）。
- 加载日志确认三引擎：HuBERT `audio[1x64000]→features[1x199x768]`、
  RMVPE `mel[1x128x416]`、Generator `feats[1x398x768]→audio[1x1x191040]`。
- **backend-T398 vs 确定性 golden（mean vs mean，同 framing）**：
  对齐 corr = **1.0000**，RMSE = 7e-5，F0 中位差 = **0.0 音分**。生产流式
  后端已逐样本复现确定性 PyTorch golden。
- file 模式：21.807s、finite、无削波，未回归。

### 残余「区别」= 随机 VAE，不可消除

锁定的 `python-reference` 用 `generator.infer()` 随机 VAE 路径；ONNX/TRT 是
确定性 mean path（cross-runtime 契约）。golden-det↔golden-rand corr 仅 0.394，
backend↔golden-rand 亦 0.395 —— backend 相对确定性 golden 的额外误差为 0，
用户听感和锁定参考的差别来自生成器 RNG，非 pipeline bug。

新增 `run_streaming_reference.py --deterministic-generator`，可复现生成
mean-path 流式基线做纯数值对拍。

### 待办

- 决定锁定标准是否同时登记确定性 golden（`...-streaming-...-DET.wav`）作为
  backend 的数值基准，随机 VAE 版作为可听基准。
- 若要在锁定参考下进一步逼近，需评估是否在导出图中保留固定种子 RNG（不推荐，
  破坏确定性）。

## 文件推理复现 + 可扩展锁定（2026-09-02 续2）

- `qiqi-zh-full.onnx` 固定 T=2380（= 本条 21.8s/16k 输入 offline 单趟的
  padded 380908/160），输出 1142400、裁 `[48000:-48000]`=1046400=参考长度。
  文件模式用它即触发 inferencer 的 full-length 单趟路径。
- 生成 `run_reference.py --deterministic-generator` 离线确定性参照
  `...python-reference-DET.wav`。
- **backend 文件模式（qiqi-zh-full） vs 离线‑DET：对齐 corr=1.0000、
  F0 中位差 0.0 音分、RMS/peak 完全相同**。整条离线链路端到端复现。
- 澄清：先前文件模式 −1.2 dB 是「模型选错走了分块路径」，非 bug；用对
  full-length 模型即逐样本一致。
- `run_reference.py` 加 `--metadata`，避免确定性复现运行覆盖可听 reference.json。

### golden 锁定 v3

`golden_manifest.json` 重构为 `context` + `standards[]`，可独立扩展：

- `offline-audible`（随机‑VAE，可听）
- `offline-deterministic`（mean‑path，文件复现目标，repro mode=file/qiqi-zh-full）
- `streaming-deterministic`（mean‑path，流式复现目标，repro mode=streaming/qiqi-zh）

`verify_golden.py` 遍历校验每条标准的 hash+format。新增 `verify_backend.py`：
驱动真实 backend（HTTP file + UDP streaming），把每条 `repro` 标准与锁定的
WAV 做包络对齐 corr / 自相关 F0 / RMS 对拍，按 manifest 容差输出 PASS/FAIL。
实测两条确定性标准均 `corr=1.0000 / F0=0.0c / rms=+0.0dB`。

### 下一步：流式收敛

file/offline 已钉死。流式当前与 offline full-length 仍差分块（每窗独立
reflect‑pad + T398 边界），目标：让流式输出收敛到 offline 结果（减小
chunk 边界与跨窗不一致）。

## Quality-first streaming Golden（2026-09-03）

离线 `offline-audible` 继续作为最高质量真值，不被流式结果替换。新增的
quality-first Python 流式基准用于提供质量上界，部署效率留给后续 C++ 优化：

- 每个 2 s target 从语句起点重新计算完整 prefix，使用固定 2 s lookahead、
  60 ms overlap，并输出无启动静音的 audition timeline。
- random-VAE 路径在每个 prefix 前恢复模型加载后的 Torch RNG state；
  deterministic 路径继续使用 mean path。
- 增加 80-sample HuBERT guard，使每窗得到完整有效 target，不再像旧 32000
  输入那样得到 95040 samples 后补 960 samples 零。
- RMVPE 全 unvoiced 窗口明确保留全零 continuous F0 / coarse pitch 1，不再依赖
  原始 pipeline 捕获 `np.interp` 空输入异常。

qiqi deterministic 相对离线 deterministic 的旧→新结果：

- log-mel correlation：0.9751 → **0.9929**
- log-mel MAE：2.97 dB → **1.64 dB**
- RMS envelope correlation：0.9553 → **0.9854**
- F0 median absolute error：10 cents → **0 cents**
- voiced IoU：0.793 → **0.883**

qiqi random-VAE 相对 `offline-audible` 的旧→新结果：log-mel correlation
0.9689 → **0.9832**，MAE 3.64 dB → **2.65 dB**，RMS envelope correlation
0.9299 → **0.9655**，voiced IoU 0.683 → **0.787**。随机 latent 仍使逐样本波形
和单次 F0 统计不能作为严格通过门槛。

同一 profile 已处理 `preprocessor/sample.mp4` 的固定前 30 s：相对其离线
Golden，log-mel correlation = **0.9772**、RMS envelope correlation =
**0.9771**、F0 median error = **10 cents**、clip = **0%**。边界样本跳变均值
0.0132，低于全局样本跳变均值 0.0200。该 30 s CPU Golden 运行约 18.8 min，
这是调试质量上界，不是生产性能结果。

## 生产 TensorRT 性能剖析与架构决策（2026-09-04）

### 当前生产基线

测试继续使用同一份 30 s 输入、同一组 full-history TensorRT engines 和真实
20 ms UDP C++ 路径。Generator 使用 FP32+TF32，HuBERT/RMVPE 使用 FP16。
当前流式契约为：

- 2 s target、2 s right context、80 samples guard
- 31040 samples / 1.94 s input hop
- full prefix 重算
- `startup_buffer_blocks=3`

实测 17 blocks、无 inference error、无 input/output overrun、无 post-start
underrun。启动欠载为 430 帧，即 8.6 s。输出质量与增加诊断前完全一致：

- log-mel correlation：`0.976944789325636`
- RMS envelope correlation：`0.9764131568715131`
- F0 median error：`10 cents`
- voiced IoU：`0.726984126984127`
- clip：`0%`

`MOZART_RVC_PROFILE=1` 增加逐阶段 wall-time 诊断，默认关闭且不改变推理数据流。
代表 blocks：

| Generator T | Prefix input | Total | F0 | HuBERT | Generator total | Generator engine |
|---:|---:|---:|---:|---:|---:|---:|
| 600 | 4.01 s | 698 ms | 294 ms cold | 27 ms | 370 ms | 353 ms |
| 1570 | 13.71 s | 1064 ms | 170 ms | 48 ms | 828 ms | 771 ms |
| 1958 | 17.59 s | 1287 ms | 202 ms | 56 ms | 1006 ms | 939 ms |
| 2928 | 27.29 s | 1945 ms | 259 ms | 83 ms | 1567 ms | 1477 ms |
| 3898 | 36.99 s | 2674 ms | 321 ms | 117 ms | 2189 ms | 2070 ms |

最后一块的其他耗时为：输入复制、归一化、高通和 reflect padding 约 41 ms，
Generator host input 准备约 119 ms，streaming target crop、crossfade 和 output
ring 合计约 0.53 ms。流式拼接不是优化重点。

当前 host buffers 是 pageable memory。Generator 日志中约 2.04 s 出现在
`cudaMemcpyAsync` D2H 调用内，是该 API 等待同一 stream 上前序 kernels 的结果，
不能解释为 2.04 s 的纯 PCIe/内存拷贝；其后的 `cudaStreamSynchronize` 仅约
0.03 ms 正好印证这一点。

当前 1.94 s deadline 在 `T=2928` 附近已经被突破，之后必然累积 backlog。
同时 full-history scheduler 在达到 `max_history_samples` 后不再产生新 blocks。
因此仅优化 allocator、crossfade 或 CUDA Graph 不能修复长期实时性。

### Generator 子图定位

`T=1650` TensorRT layer profile 的可归因 kernel 总和为 788.45 ms：

- encoder/conditioning：79.48 ms，10.08%
- flow：9.80 ms，1.24%
- NSF decoder：699.16 ms，88.68%

qiqi Generator 使用 `[12,10,2,2]` 上采样，总倍率 480。按实际 ConvTranspose、
ResBlock1 最大 kernel/dilation 和 `conv_pre/conv_post` 反推，目标音频对 latent
的理论依赖约为左右各 10 frames，即每侧约 100 ms。

隔离 PyTorch 实验保持完整 `enc_p + flow` 结果不变，只裁 latent 和同位置的
预计算 harmonic source。对于 2 s / 200-frame target：

| Decoder context / side | Relative RMSE | Correlation |
|---:|---:|---:|
| 0 frames | 17.66% | 0.98439 |
| 4 frames / 40 ms | 1.22% | 0.999925 |
| 8 frames / 80 ms | 0.0732% | 0.99999973 |
| 10 frames / 100 ms | 0.0729% | 0.99999973 |
| 16 frames / 160 ms | 0.0729% | 0.99999973 |

残余微差来自不同 tensor shape 下的 convolution 算法/浮点累加，而不是缺少远端
上下文。完整预计算 source 再走完整 decoder 与原 `_decode` 逐样本相同。

这个结果证明：保留完整分析上下文并不要求把整段历史重新解码为波形。以
16 frames/side 为保守值。完整 2 s target 是 200 frames，因此对应 decoder
输入为 `200 + 2*16 = 232` frames；已经导出和实测的 `T=226` prototype 使用的
实际契约是 `200 + 2*13 = 226`。194 frames 是 crossfade 后的 emit hop，不能与
decoder target 混用。按现有 profile 线性估算，decoder 从约 699 ms 降到约
96 ms；必须以实际拆分后的 TensorRT engine benchmark 为准。

### 推荐架构

目标不是只勉强通过 4 s，而是满足以下不变量：

- 算法延迟有固定上界，不随会话时长增长
- 每 hop 工作量固定，p95 明显低于 1.94 s
- 正常运行只需一个 ready block，不靠多块 reserve 掩盖 backlog
- 每个拆分阶段都能与 PyTorch/当前 TensorRT Golden 独立对拍

#### 1. Bounded overlap-save scheduler

- 初始默认 12 s left context、2 s target、0.5 s right context、60 ms overlap。
- 每个 job 通过绝对 sample range 描述 analysis、target 和 emit 区间。
- 达到上限后滚动丢弃旧历史，不再使用从语句起点增长的 prefix，也不因
  `max_history_samples` 停止。
- 第一块完成即开始播放，`startup_buffer_blocks=1`。
- 先保持 1.94 s hop，以最低计算/功耗实现小于 4 s；不要为追求更低首包延迟
  立即改成 0.5 s hop，因为它会把完整 bounded front 的调用频率提高近四倍。

当前 full Generator 在 `T≈1650` 的 C++ 估算约 1.0--1.1 s，所以仅 bounded
方案预计启动约 `2.5 + 1.1 = 3.6 s`，可以作为低风险中间里程碑，但余量不足，
不作为最终极限效率架构。

#### 2. Split Generator and target-only decode

拆为两个 TensorRT contracts：

1. Generator front：完整 bounded `feats/pitch/latent_noise/sid` -> `z`，包含
   `emb_g + enc_p + sampling + reverse flow`。
2. NSF decoder：裁后的 `z`、连续 `pitchf`、speaker conditioning、相位连续的
   source/noise -> 局部 waveform。

front 保留完整 bounded 上下文。第一版只在 flow 之后裁 `z`，因为 flow 仅占
1.24%，没有必要先承担在 coupling layers 内证明 crop 等价的复杂度。decoder
输入使用 target 左右各 16 latent frames，输出后再裁掉各 160 ms context。

source 不能在局部窗口任意重置相位。实现应选择以下一种可验证契约：

- front/source graph 输出完整 harmonic source，decoder 接受裁后的 source；或
- 持久化绝对 phase accumulator，并按 target 的绝对 RNG offset 生成 source。

原型阶段可以让 `z` 经 host 返回再送 decoder，因为它只有约 1.3 MB；最终版本
必须支持 device-to-device tensor view，避免不必要的 D2H/H2D 和同步。

按 layer profile 的初始预算：front 约 90 ms，226-frame TF32 decoder 约
100 ms，F0+mel 约 150--180 ms，HuBERT 约 50 ms，host preprocessing 小于
30 ms。即使先串行，单块也应在约 0.4--0.5 s 级别；启动约 3.0 s，稳态预算
约为 hop 的四分之一。

独立 `T=226` decoder 原型已经验证这个预算：

- PyTorch -> ONNX：correlation `1.0`，relative RMSE `0.000235%`
- ONNX -> TensorRT TF32：correlation `0.999999624`，relative RMSE `0.0867%`
- Orin TensorRT + CUDA Graph：GPU median `98.05 ms`、p95 `99.21 ms`、
  p99 `99.94 ms`
- engine 约 61 MiB，execution-context device memory 约 80 MiB

FP16 decoder 构建已成功解析模型并开始生成候选 engine，但完整
builder-optimization-level 5 在 10 分钟诊断命令限制内未完成，留下的 0-byte
文件不是有效 engine，不能据此判断 FP16 的性能或音质。

#### 3. Device-resident asynchronous runtime

当前 `IEngine::run()` 强制返回 `std::vector<float>` 并同步整个 stream。最终
runtime 应改为显式任务图：

- reusable `GpuTensor` / tensor view 和按最大固定 shape 预分配的 buffers
- pinned host staging，仅在真正需要 CPU 数据时传输
- `enqueue` 返回 event/future，而不是每个 engine 内部立即 synchronize
- HuBERT 和 mel/RMVPE 两条独立分支并行，Generator front 等待二者 event
- front 的 `z` 通过 D2D slice/view 直接交给 decoder
- 固定 bounded profiles 后捕获 CUDA Graph，消除动态 enqueue host 开销
- warm up 所有实际 shape，而不是只 warm up 一个邻近 shape

Generator 必须等待 HuBERT 与 pitch；但 HuBERT 和 RMVPE 在共同预处理完成后
没有相互数据依赖，当前串行是 runtime API 限制，不是模型限制。

#### 4. Split precision and remaining kernels

整图 FP16 曾使相关性降到约 0.586，不能采用。拆图后可以单独测试：

- front 保持 FP32+TF32
- decoder-only engine 尝试 FP16，并直接与 TF32 decoder target 对拍
- 只有 decoder crop 和端到端音质同时过门槛才启用

后续优先级依次为：GPU/source RNG 复用、RMVPE decode/pitch quantization 上 GPU、
GPU mel 或持久 CPU worker、消除 disabled-index feature copy。INT8、DLA 和模型
蒸馏属于更后面的质量风险项。

### 是否需要替换模型架构

小于 4 s 不需要先重训 causal RVC。现有权重通过 bounded front + target-only
decoder 已有充足预算。直接替换为 causal HuBERT、流式 pitch 网络或新 Generator
会改变语义并失去当前 Golden 的强对拍能力，只有出现以下情况才进入该路线：

- 0.5--1.0 s right context 的最终音质始终不能过门槛
- target-only decoder 的真实 TensorRT 性能与 layer profile 估算严重不符
- 产品目标进一步变为小于约 1.5 s，且允许重新训练/蒸馏

### 分阶段验收门槛

1. Split ONNX：相同 captured tensors 下 front 与 PyTorch 对齐；完整 decoder 与
   原 Generator 对齐；cropped decoder correlation 至少 0.99999、relative RMSE
   不高于 0.1%。
2. TensorRT：至少两个 bounded sequence lengths 成功；报告 shape、dtype、finite、
   relative error、correlation、duration 和音频统计，不以“engine 能加载”为通过。
3. Streaming quality sweep：left `8/12/16 s` × right `0.5/0.75/1.0 s`。候选相对
   当前生产基线的 log-mel/RMS correlation 回退不超过 0.0005，F0 median 不高于
   10 cents，voiced IoU 回退不超过 0.01，clip 保持 0%，边界指标不恶化。
4. Performance：启动 p95 小于 4 s；目标架构进一步要求 block p95 小于 0.5 s；
   连续 10 分钟 `late_blocks=0`、post-start underrun=0、overrun=0、error=0。
5. 每次只替换一个阶段，并保留原输入、Golden、split ONNX 和 backend WAV 供并排
   试听；任何上游 tensor mismatch 都不能用 RMS/filter 参数补偿。

## 主流实时 RVC 契约复核与路线修正（2026-09-04）

此前推荐架构仍把 full-history + future-lookahead quality ceiling 当成产品实时
契约，因此虽然比 8.6 s 基线更快，仍然远慢于现有实时 RVC。对上游实现复核后，
应保留上述数据作为 correctness/quality 上界，但停止把 1.7--2.0 s future
lookahead 作为实时 backend 的前提。

复核来源：

- [RVC 官方 realtime GUI](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/blob/main/realtime_gui.py)
  默认使用 0.25 s block、0.05 s crossfade 和 2.5 s extra。其界面算法延迟为
  audio-device latency + block + crossfade + 10 ms；extra 不计入等待。
- [RVC 官方 realtime inferencer](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/blob/main/infer/rtrvc.py)
  把 extra 作为滚动的过去音频，缓存 pitch，只为最新 block 提取 F0，并通过
  `skip_head/return_length` 在 Generator 内裁掉历史 decoder 工作量。
- [w-okada VCClient 文档](https://github.com/w-okada/voice-changer/blob/master/tutorials/tutorial_rvc_en_latest.md)
  明确将可感延迟定义为 `buf + res`；`EXTRA` 是提高转换精度的过去音频，仅增加
  inference work，不增加 future buffering。
- [Applio realtime core](https://github.com/IAHispano/Applio/blob/085197e7/rvc/realtime/core.py)
  同样使用短 block、滚动 history、pitch buffer、预分配 buffer 和 SOLA/phase
  alignment。
- [LLVC](https://koeai.github.io/llvc-demo/) 报告的 20 ms 以下延迟来自以 RVC 为
  teacher 蒸馏的新流式网络，不是原 RVC checkpoint 的普通部署优化。

新增 `run_realtime_reference.py`，在不启动音频设备 GUI 的情况下直接调用固定
commit `81eed5e8f68b6bed1789f682fe78cdd324495afc` 的上游 `rtrvc.RVC.infer()`，并
复现 rolling history、pitch cache、Generator crop 和 SOLA。锁定 qiqi 输入的
CPU 结果如下；CPU 时间只验证语义，不代表 TensorRT 性能：

| Block | Past extra | Fixed input | CPU median/block | Log-mel vs offline | RMS corr | F0 median | Voiced IoU |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 250 ms | 2.5 s | 2.81 s | 2811 ms | 0.96494 | 0.93761 | 20 cents | 0.67075 |
| 500 ms | 2.5 s | 3.06 s | 3760 ms | 0.96600 | 0.93151 | 10 cents | 0.66399 |

两条输出时长均为 21.8 s、finite、clip 0%。试听文件为：

- `/tmp/opencode/qiqi-upstream-realtime-250ms-25s.wav`
- `/tmp/opencode/qiqi-upstream-realtime-500ms-25s.wav`

这些指标证明两个事实：主流 RVC 的百毫秒延迟是真实架构结果，不是把 2 s future
lookahead 隐藏在口径外；同时它并不与离线输出数值等价。500 ms block 也没有显著
恢复离线 correlation，因此继续只扫 target/right-context 不能解决产品问题。

### 修正后的 backend 方向

第一版应忠实复现 upstream realtime contract，而不是继续缩短 quality-first
future-lookahead contract：

1. 250 ms target block、2.5 s rolling past、50 ms crossfade、10 ms SOLA search，
   不等待 future audio；历史在启动时补零。
2. HuBERT 处理固定 rolling window；RMVPE 只处理新 block 加算法所需边界，并把
   coarse/continuous pitch 写入固定 cache。
3. Generator encoder 读取 bounded history，但 flow/decoder 只保留最新 target、
   crossfade 和 SOLA search。现有 split exporter 和 target-only decoder benchmark
   可复用，但 shape 应从旧的 200-frame target 改成 realtime `return_length`。
4. 使用 normalized-correlation SOLA 选择 overlap offset，而不是固定位置线性
   crossfade；预分配 ring、pitch、engine 和 output buffers。
5. 性能验收区分 input buffering、真实 end-to-end latency 和纯 inference time，
   不能再用后两者互相替代。

对于 250 ms 默认值，理论输入等待从 4 s 降为 250 ms。`T=226` decoder 已在 Orin
实测约 98 ms，而 realtime decoder 只有约 31 frames；加上短窗 HuBERT、局部 RMVPE
和 front 后，合理目标是先做到 250--500 ms 端到端，再基于试听决定 block/extra
默认值。离线 Golden 继续用于逐阶段正确性和回归检测，但不再要求实时输出与离线
波形达到原先近乎相同的 correlation；实时质量必须新增 upstream parity、边界指标
和盲听门槛。

## 上游短块 TensorRT 落地（2026-09-05）

### 固定契约和资产

20 ms 网络帧继续保持不变，backend 每 12 帧聚合一个 240 ms 推理块。为避免
250 ms 与 20 ms transport frame 不整除，实际固定契约为：

- 新 block：`3840@16k = 240 ms`
- 滚动过去：`40000@16k = 2.5 s`
- crossfade：`2400@48k = 50 ms`
- SOLA search：`480@48k = 10 ms`
- analysis：`44800@16k = 280 Generator frames`
- Generator crop：`skip_head=250`、`return_frames=30`
- decoder 输出：`14400` samples；每块最终 emit：`11520` samples

对应固定 TensorRT 特征资产为 HuBERT `[1,44800] -> [1,139,768]` 和
RMVPE `[1,128,32] -> [1,32,360]`。Generator 已拆为：

- front：`[1,280,768] -> z[1,192,30]`
- decoder：`z[1,192,30] -> audio[1,1,14400]`

相同 captured tensors 下，front TensorRT/PyTorch correlation 为
`0.999999976`，decoder 为 `0.999999631`。独立 `trtexec` median 为 HuBERT
约 `22.06 ms`、RMVPE `11.84 ms`、front `6.69 ms`、decoder `15.84 ms`。

### Quality/realtime 资产隔离

原实现只接受 `rvc.hubert_path` 和 `rvc.rmvpe_path`。若它们指向固定
`44800/32` 导出，file/quality 的可变长输入会错误回退到同一固定 ONNX。
现已新增：

```yaml
rvc:
  hubert_path: ./assets/hubert/hubert_base.onnx
  rmvpe_path: ./assets/rmvpe/rmvpe.onnx
  realtime_hubert_path: ./assets/hubert/hubert-realtime.onnx
  realtime_rmvpe_path: ./assets/rmvpe/rmvpe-realtime.onnx
```

`infer()` 只使用 quality 资产；`infer_realtime()` 只使用 realtime 资产。
Realtime capability 只有在两份专用资产实际加载为 TensorRT、固定输入 shape
正确、零输入预热输出 shape 正确且全部 finite 时才成立。专用资产缺失或校验失败
只会禁用短块模式，不影响 file/quality 路径。

同一 daemon 中已先加载两套特征引擎并完成两类真实请求：

- `rt_rvc/qiqi-zh-realtime` 明确选择 `upstream realtime`，未使用 quality engine。
- 切换至 `file_rvc/qiqi-zh` 后，1 s 文件请求走 quality dynamic ONNX fallback；
  CUDA EP 因 sm87 kernel 不兼容失败后按既有逻辑重建 CPU session，任务仍成功，
  输出 finite、peak `0.76092`、clip `0%`。该请求未误用固定 realtime engine。

### 30 秒端到端结果

输入取自 `preprocessor/sample.mp4` 前 30 s：

- 16 kHz 输入：`rvc-golden/input/preprocessor-sample-mp4-first-30s.wav`
- 输入 WAV SHA-256：`d7dafb12f8d444b2270f2d06158f47c235ca7892aaca1513561dc9971f2a8654`
- 上游同契约输出：`rvc-golden/output/preprocessor-sample-mp4-first-30s-qiqi-upstream-realtime.wav`
- 上游输出 SHA-256：`d06582925a7637f3e286d7885a0e52848a6a59799efe7c7268434814c74f915e`
- backend 输出：`rvc-golden/output/preprocessor-sample-mp4-first-30s-qiqi-zh-realtime-trt.wav`
- backend 输出 SHA-256：`b11a9617a6e3efab32f50348f6c5b08f371663841a9558aa1b69ae6cb93ee171`

UDP/backend 状态：`137` blocks、`0` inference error、`0` late block、`0`
input/output overrun、`0` reset、`0` 丢包。启动时 `16` 个 20 ms underrun 即
`320 ms`，与 240 ms 输入聚合加约 90 ms 推理的预期一致；启动后没有欠载。

137 个 C++ blocks 的性能分布：

- realtime pipeline：median `88.19 ms`、p95 `93.25 ms`、max `95.15 ms`
- 含 scheduler/SOLA/output ring：median `91.09 ms`、p95 `96.56 ms`、max
  `98.57 ms`
- 固定 deadline：`240 ms`

backend 试听输出为 48 kHz mono float，`1,439,040` samples / `29.98 s`，全部
finite，peak `0.83710`、RMS `0.08531`、clip `0%`。相对固定 commit 上游
realtime CPU 输出，在共同的 29.98 s 时间线上：

- alignment lag：`0 ms`
- log-mel correlation：`0.98294`
- log-mel MAE：`2.46 dB`
- RMS envelope correlation：`0.98545`
- RMS ratio：`-0.03 dB`
- F0 median / p90：`20 / 60 cents`
- voiced IoU：`0.77989`
- clip：`0%`
- block-boundary spectral step：`2.566 dB`，低于全局 `2.696 dB`
- block-boundary sample jump：`0.01959`，低于全局 `0.02301`

相对 offline Golden 的 log-mel correlation 为 `0.94850`、RMS envelope
correlation 为 `0.91811`。这符合上游 realtime 本身不与 offline 波形等价的既定
结论；短块正确性应以上游 realtime parity、边界指标和试听为准。

### 试听验收结论

本次试听确认合格。按实际首块运行记录，从第一帧输入到第一帧变声输出约
`320 ms`；后续稳定推理明显低于 `240 ms` block deadline，满足当前 realtime
体验目标。
