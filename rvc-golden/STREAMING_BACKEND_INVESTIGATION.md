# Golden 与流式 Backend 调查日志

最后更新：2026-09-01

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
- Golden runner SHA-256：`6e65f75aad68127c6a409dd5d87e6c98c6ddd592acd1e73a63abb830adc31540`
- Streaming runner SHA-256：`6b2753cca21526c763dd2c9e613c5fd172393874e6a12a28b562e8627faf0c28`
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
