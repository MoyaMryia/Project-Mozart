# de_narrator 对齐与流萤回归

## 结论

2026-10-26，Mozart 的 `de_narrator` 已切换到经过 Golden 对拍的 split 实时资产。
流萤保留原有资产，并通过加载器、文件输出和实时链路回归。
完整数值与实际运行资产见 [verification.json](verification.json)。

## 问题定位

旧 `de_narrator.onnx` 的 351 个可直接比较权重中，104 个与 `de_narrator_clean.pth` 不同。
这 104 个权重全部属于原 weight-norm 模块，包含 Generator flow 和 decoder 卷积。
`emb_g.weight` 等其余 247 个权重一致。

clean checkpoint 保存 `weight`。
原始 RVC 模块初始化后需要 `weight_g` 和 `weight_v`。
官方加载代码使用 `strict=False`，并忽略返回的缺失键和多余键。
这种加载方式会保留未加载的初始化参数。
原有 Golden 加载器则明确拒绝该格式。

旧模型目录还缺少 `de_narrator-front.onnx` 和 `de_narrator-decoder.onnx`。
因此，先前的实时测试实际进入旧 2 秒窗口路径。
流萤使用的路径是 `240 ms` 块、`2.5 s` 历史上下文和 SOLA。

## 修复

`tools/export_generator_onnx.py` 统一处理 plain 和 weight-norm 两种权重格式。
对于 plain 权重，加载器先移除对应模块的 weight norm，再加载权重。
对于标准权重，加载器先加载 `weight_g/weight_v`，再移除 weight norm。
缺失推理权重、多余权重和冲突格式均使加载失败。
Golden 文件运行器复用该加载器。

Jetson 的 `rvc-backend/models/de_narrator/` 使用真实目录和逐文件软链。
旧同名 ONNX 与优先加载的 `.engine` 已移到 `legacy/`。
实时路径加载新导出的 front 和 decoder TensorRT 引擎。
文件入口 `models/de_narrator/de_narrator.onnx` 软链到本目录的 `de_narrator-dynamic.onnx`，使用整段上下文。
验证摘要的 `de_narrator_file_asset` 记录入口、解析路径、SHA-256 和实际运行时。
动态导出已通过 `T=100/200/202` 测试，并通过 `T=1144` 的完整 Golden 捕获张量测试。

固定 `T=200` 的文件路径复测暴露了分块差异。
其 log-mel 相关性为 `0.418`。
动态文件路径将该值提高到 `0.988`。

## 同张量验证

验收沿用现有条件：相对 RMSE ≤ `0.001`，余弦相似度 ≥ `0.99999`。
下表的相对 RMSE 使用百分数。

| 路径 | de_narrator | 流萤 |
| --- | ---: | ---: |
| PyTorch → ONNX split 串联 | 0.000743% | 0.000559% |
| PyTorch → C++ TensorRT front | 0.0000703% | 0.000110% |
| PyTorch → C++ TensorRT decoder | 0.00443% | 0.00214% |

`de_narrator` 完整捕获张量的动态 ONNX 相对 RMSE 为 `0.01124%`。
C++ TensorRT HuBERT 和 RMVPE 的相对 RMSE 分别为 `0.0504%` 和 `0.0618%`。
上述测试全部通过原有数值条件。

## 整段实时对照

对照使用同一个 espeak-ng 输入，以及相同的块、历史、交叉淡化和 SOLA 搜索配置。
PyTorch 对照运行原始 RVC realtime 算法。
`de_narrator` 的上游运行使用转换后的标准 weight-norm checkpoint。
该转换与 clean 权重的最大绝对差为 `1.19e-7`。
流萤直接使用原始 checkpoint。

| 指标 | de_narrator | 流萤 |
| --- | ---: | ---: |
| log-mel 相关性 | 0.9880 | 0.9824 |
| RMS 包络相关性 | 0.9919 | 0.9708 |
| RMS 比值 | +0.055 dB | -0.071 dB |
| F0 中位偏差 | 10 cents | 10 cents |
| F0 第 90 百分位偏差 | 20 cents | 30 cents |
| UDP 收发包数 | 623/623 | 623/623 |
| 推理块数 | 51 | 51 |
| 推理错误、晚到块、丢包 | 0、0、0 | 0、0、0 |

两条链路均有 20 帧启动补零，稳态欠载为 0。
状态中的 `playout_delay_ms` 均为 `400`。
质量统计使用 log-mel、RMS 包络和 F0；Generator 数值验收使用相同的捕获张量及合成噪声。

8 s 二次变声样本也完成了 `de_narrator` 对照，来源更正见文末适用范围。
旧实时输出与离线 Golden 的 log-mel 相关性为 `0.4609`。
修复后的实时输出为 `0.9481`，动态文件输出为 `0.9883`。

## 流萤回归

修复前后的加载器生成了完全相同的 353 个流萤权重张量。
相同输入和合成噪声生成的 96000 个输出样本逐样本一致。

流萤当前文件输出与本轮修复前保存的 `liuying-file-natural-test.wav` 逐样本一致。
比较覆盖全部 384000 个样本，最大绝对差为 `0`。
流萤的三个 ONNX 文件和两个 TensorRT 引擎的 SHA-256 均保持不变。
验证摘要的 `liuying_asset_regression` 逐文件记录修复前后的哈希。

## 复现与试听

在 Jetson 仓库根目录执行以下命令。
配置 `backend-ui.yaml` 使用端口 `18000/18080`。
配置 `backend.yaml` 使用端口 `18101/18181`。

```bash
./build-gpu/state/mozart_stated rvc-golden/de-narrator-run/backend-ui.yaml
```

在另一个终端运行实时文件测试。
测试通过 UDP 发送音频，HTTP 仅用于控制。

```bash
/home/moyamryia/vc_backend_venv/bin/python tools/stream_audio_udp.py \
  rvc-golden/de-narrator-run/de-narrator-espeak-input.wav \
  /tmp/de-narrator-repeat.wav --model-id de_narrator \
  --flush-seconds 3 --trim-leading-underruns --trim-to-rvc-duration
```

测试结束后，将模式切换为 `idle`。
再次运行该命令时，将 `--model-id` 改为 `liuying`，并更换输出文件名。

两端的本目录包含以下试听文件。

- `de-narrator-espeak-input.wav`：共同输入。
- `de-narrator-espeak-python-reference.wav`：de_narrator 离线 Golden。
- `de-narrator-espeak-onnx-generator.wav`：完整 Golden 张量驱动的 ONNX 输出。
- `de-narrator-espeak-python-realtime.wav`：de_narrator PyTorch 实时对照。
- `de-narrator-espeak-mozart-backend.wav`：de_narrator Mozart 实时输出。
- `liuying-espeak-python-reference.wav`：流萤离线 Golden。
- `liuying-espeak-python-realtime.wav`：流萤 PyTorch 实时对照。
- `liuying-espeak-mozart-backend.wav`：流萤 Mozart 实时输出。
- `de-narrator-natural-python-reference.wav`：原自然语音的 Golden 输出。
- `de-narrator-natural-mozart-before.wav`：原自然语音的旧实时输出。
- `de-narrator-natural-mozart-aligned.wav`：原自然语音的新实时输出。

本轮结束时，后端和麦克风进程均已停止。

## 适用范围

- 配置：Jetson Orin Nano Super 8GB；16 kHz 输入、48 kHz 输出；两个模型均为 RVC v2、F0、768 维特征。
- 音色：`de_narrator` 使用 109 个 speaker 中的 ID 0；流萤使用 ID 0。
- 参数：pitch `0`、index rate `0`、RMS mix `1`、protect `0.33`；文件与 UDP 测试均跳过 RNNoise。
- 样本：一份约 9.46 s 的 `espeak-ng -v en-us -s 145` 合成输入，两模型复用；另有一份 8 s 二次变声样本。
- 输入来源更正：`/tmp/rvc-say.wav` 和 `RVC-official/test-input/input.wav` 来自七七变声输出 `full-run-20261005/natural-qiqi-realtime-trimmed.wav` 的前 8 s。对应的原始录音为 `rvc-golden/input/preprocessor-sample-mp4-first-30s.wav`。
- 实时统计：PyTorch 输出为 9.45 s；UDP 工具按离线 RVC 帧定义裁成 9.44 s；质量统计覆盖共同的 9.44 s。
- 测试：主机 420 项单元测试完成，其中 5 项因缺少 PyTorch 跳过；Jetson 实际执行并通过这 5 项权重加载测试。
- 文件运行时：de_narrator 动态 Generator 使用 ONNX Runtime CPU；状态接口记录 CUDA EP 的设备内核不兼容降级原因。
- 人工听感确认：阻塞：当前工具没有音频试听能力；解除条件：用户试听共同输入与两模型的 PyTorch、Mozart 输出。
