# Pocket 参考音色播报的 CUDA 迁移

日期：2026-10-08。回放应用提交：`23ca984`。设备端 Python 工具摘要与该提交一致。
构建另外包含后续 CMake 修复和内部复制算子；具体差异及二进制摘要见 [gpu-results.json](gpu-results.json)。

CUDA 路径已在 Jetson 实际生成音频，但本轮没有获得延迟收益。
固定文本对照中，CUDA float32 的合成耗时中位数为 2.506 s，原有 CPU int8 为 1.779 s。
实时回放的播报覆盖率也从 20／23 降至 18／23。因此，通用启动参数继续默认使用 CPU int8。
独立启动器明确选择 CUDA float32，供后续复测使用。
后续五步合并、线程数和连续性对照见 [保留连续性的延迟优化](CONTINUITY.md)。

## 迁移范围

本次为 Pocket TTS 增加 CUDA 推理路径。字幕保留原有 CPU 运行库。Qwen 0.8B 翻译保留原有 CUDA 路径。
监督程序只向播报服务设置独立 Python 包目录。生成步数为 5，随机种子为 42，CPU 线程数为 2。

`provider_requested` 表示请求的执行层。`available_providers` 表示运行库包含的执行层。
这两个字段均不能证明模型已经执行 GPU 内核。
`onnxruntime_library` 通过 C API 函数地址识别实际运行库；`onnxruntime_loader_library` 标识兼容接口库。
若实际运行库只有 CPU 执行层，CUDA 请求会在模型初始化前失败。

## 设备与资产

| 项目 | 配置 |
| --- | --- |
| 设备型号 | NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super |
| GPU 架构 | `sm_87` |
| 内存 | 约 7.4 GiB 可见内存，约 13 GiB 交换区 |
| 系统 | L4T 39.2 |
| CUDA 编译器 | 13.2.78 |
| 电源模式 | `MAXN_SUPER`，模式编号 2 |
| Python / sherpa-onnx | 3.12.3 / 1.13.6 |
| 原有播报运行库 | ONNX Runtime 1.27.1，仅 CPU 执行层 |
| 专用 CUDA 运行库源码 | ONNX Runtime 1.29.0，`2e2543fbe9fae542f921d47a72d21d5a4ef0b710` |
| 资产 | 官方 `sherpa-onnx-pocket-tts-2026-01-26` 浮点包 |

浮点包来自 [sherpa-onnx 官方发布](https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/sherpa-onnx-pocket-tts-2026-01-26.tar.bz2)。
压缩包大小为 168,148,625 B。下载端与设备端的 SHA-256 相同：

```text
61422a478ef09d9b2c067261fb822e11786e0c86842c76d8633b7069968be7b5
```

`encoder.onnx` 和 `text_conditioner.onnx` 与原有 int8 包逐字节相同。
另外三个文件改用浮点权重。性能对照会记录这次精度变化。

## 隔离构建

设备目录为 `~/mozart-realtime-patches-20261008/gpu-runtime`。
原有仓库、Python 环境和模型资产保持原状。
安装脚本复制 sherpa 包，再替换副本中的 CPU 库。
一个小型 C API 兼容接口连接原有 `VERS_1.27.1` 绑定与新运行库。
新运行库报告自己的实际版本；兼容接口不修改版本字符串。

编译目标为 `CMAKE_CUDA_ARCHITECTURES=87`。总并行任务数为 4，CUDA 执行层的编译池大小为 2。
构建关闭扩展算子、机器学习算子、Flash Attention、内存优化注意力和遥测。
已检查的模型使用 ONNX 标准域 opset 14，且不含 `Attention`、`RotaryEmbedding`。
专用构建排除这些未使用的 CUDA 代码，修复当前上游配置下的编译错误。
CPU 执行层保留卷积所需的共享激活实现。
算子裁剪配置另保留 `MemcpyFromHost` 和 `MemcpyToHost`；CUDA 分图会插入这些内部算子。
该运行库的适用范围为本次核对的浮点 Pocket 模型。

依赖下载直接使用 GitHub 的 `codeload.github.com` 地址。上游校验和保持原值。
构建脚本见 [build_pocket_cuda.sh](../../rvc-golden/realtime-patches-20261008/build_pocket_cuda.sh)。
独立包与启动器脚本见 [install_cuda_overlay.py](../../rvc-golden/realtime-patches-20261008/install_cuda_overlay.py)。

## 验证范围

已完成：104 项 Python 回归测试；真实 CPU 库拒绝 CUDA 请求；兼容接口转发与实际库路径识别。
另外，独立 `sm_87` CUDA 内核和 cuBLAS 矩阵乘法已通过。
`cuobjdump` 在新 CUDA 执行层中列出 62 个 `sm_87` ELF 映像。
测试源码见 [cuda_sm87_probe.cu](../../rvc-golden/realtime-patches-20261008/cuda_sm87_probe.cu)。
PyTorch 的 CUDA 逐元素加法与矩阵乘法也已通过。未测试完整 Pocket PyTorch 路径。
不能只根据架构列表或执行层列表判定完整模型可用。

性能对照使用此前回放的 8 条首片段文本。文本是自动译文，不是人工确认译文。
每种配置运行两轮，第二轮的 8 个任务作为热启动样本。
对照包含原有 CPU int8、原有 CPU 浮点、新运行库 CPU 浮点、新运行库 CUDA 浮点。
所有配置使用相同参考 WAV、生成步数、种子、线程数和时长上限。

`synthesis_seconds` 从工作进程开始处理任务计时，到 WAV 写入完成结束。
该指标包含参考 WAV 读取、模型推理和文件写入。
首回调发生在生成过程中，不能替代首片段可播放时间。
算子执行记录单独采集，不用于无分析开销的延迟统计。
本次保留了五个会话文件，文件名包含不同的毫秒时间戳。
节点事件计数只覆盖这五个文件和诊断运行的 8 条文本，不代表无分析运行的耗时分布。

实时对照使用保留的 120 s 预处理输入，不再执行 RNNoise。
翻译模型为 Qwen 0.8B Q4_K_M，输出上限为 128 token。
RVC 关闭。ALSA `null` 按生成音频时长等待。
这些计时不包含物理扬声器延迟。

构建出现内存压力后，按用户许可停止了 `display-manager`。
本轮固定文本对照和 GPU 回放均在图形服务停止时运行。
测试结束后，图形服务已重新启动；已确认服务状态为 `active`。

## 实测结果

### 固定首片段

每种配置提交 16 个任务，均完成 16／16。下表只统计第二轮的 8 个任务。
各配置顺序执行，未做随机交叉重复试验。
计时起止点为工作进程开始处理任务至 WAV 写入完成。
实时因子为每条任务的合成耗时除以输出音频时长，再取中位数。

| 配置 | ONNX Runtime | 合成耗时中位数 | 最大值 | 实时因子中位数 |
| --- | --- | --- | --- | --- |
| 原有 CPU int8 | 1.27.1 | 1.779 s | 2.496 s | 0.741 |
| 原有 CPU float32 | 1.27.1 | 2.772 s | 4.025 s | 1.101 |
| 新运行库 CPU float32 | 1.29.0 | 2.798 s | 4.253 s | 1.111 |
| 新运行库 CUDA float32 | 1.29.0 | 2.506 s | 3.599 s | 1.012 |

CUDA 相对同运行库的 CPU float32，中位数减少约 10.4%。
但相对现有 CPU int8，中位数增加约 40.9%。这不是现有部署的加速方案。
int8 与 float32 会产生不同音频，不能将差异全部归因于执行设备。
两者第二轮输出时长中位数分别为 2.40 s 和 2.48 s；实时因子也没有显示 CUDA 优势。

所有固定文本输出的样本均为有限值。峰值未达到满幅。
三个 CPU 配置的重复 WAV 均为 8／8 逐字节相同；CUDA 为 5／8。
固定种子没有保证 CUDA 路径逐字节确定性。
本轮未做人工听检，不能据此认定音色、发音或语义质量等同。

原始记录和试听文件位于 `rvc-golden/realtime-patches-20261008/pocket-gpu-v2`。
同一文本的示例为 `cpu-int8/01-00.wav`、`cpu-float-new-runtime/01-00.wav` 和 `cuda-float/01-00.wav`。

### GPU 执行证据

独立诊断运行完成 8／8 条文本。五个分析文件记录了 467,307 个 CUDA 执行层节点事件。
其中包含 31,464 次 `Gemm`、9,997 次 `MatMul`、174 次 `Conv` 和 72 次 `ConvTranspose`。
因此，结论不只来自执行层列表或初始化日志。
这些是 ONNX Runtime 的节点事件计数，不是硬件内核总数或设备时间占比。

同一记录还包含 131,220 个 CPU 执行层节点事件，以及 9,772 个 `MemcpyFromHost` 节点事件。
大量小算子、跨执行层工作和状态传输可能抵消 GPU 收益。这是待定位的原因，尚未测得各项耗时占比。
[sherpa 的 Pocket 实现](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.6/sherpa-onnx/csrc/offline-tts-pocket-model.cc)
在生成循环中多次调用会话并接收状态张量。
本补丁只增加 CUDA 执行层选择，尚未实现状态张量常驻 GPU 或 I/O Binding。

### 120 s 实时回放

下表与此前 `limit128-v1` CPU 回放比较。两组均只运行一遍。
输入、参考 WAV、翻译模型和原生二进制的摘要相同。
23 条最终识别文本、21 条成功译文和两条翻译拒绝原因相同。
CPU 历史回放保留图形服务；GPU 回放停止图形服务。
两组的精度和获准播报集合也不同，因此不能将中位数差当作逐句加速或减速量。

| 指标 | CPU int8，`limit128-v1` | CUDA float32，`gpu-v1` |
| --- | --- | --- |
| 翻译计算中位数／最大值，成功 21 条 | 0.347／3.650 s | 0.623／3.723 s |
| 最终识别发出至首播，中位数／最大值 | 10.479／24.094 s | 12.889／26.187 s |
| 播报提交至首播，中位数／最大值 | 9.934／23.768 s | 12.063／25.775 s |
| 首片合成中位数／最大值 | 1.785／3.843 s | 2.511／5.357 s |
| 已准入播报完成数／准入数 | 20／20 | 18／18 |
| 完成播报数／最终识别句数 | 20／23 | 18／23 |
| 容量限制拒绝的句序号 | 21 | 19、20、21 |
| 已完成播报总时长 | 121.28 s | 123.36 s |

共同完成的 18 句中，CUDA 的提交至首播耗时均增加。
逐句增加量的中位数为 2.301 s，平均值为 2.576 s，范围为 0.450–5.973 s。
翻译与播报同时使用 GPU，可能产生资源竞争；本轮没有单独测量竞争占比。

GPU 回放产生 269 次临时字幕更新。识别事件发出至 SSE 接收的中位数为 100 ms，最大值为 201 ms。
这些计时不含语音至识别、浏览器渲染或实体扬声器延迟。
6,000 帧采集完成，输入溢出为零。字幕修订和 46 个片段的播放顺序正确。
18 份完整 WAV 的 PCM 均与片段连接结果一致，int16 满幅样本数为零。

最低可用内存为 3.226 GiB。测试开始和结束时，交换区使用量均为 176,201,728 B。
测试没有增加换出页数，换入为 130 页；不能将已有交换区占用算作本轮新增。
GPU 利用率每 1 s 采样一次，共 153 个样本。中位数为 62%，平均值为 58.2%，最大值为 99%。
此利用率包含翻译与播报，不能视为 Pocket 独占压力测试。
测试程序以状态码 0 退出，本轮子进程均已退出。

汇总数据见 [gpu-results.json](gpu-results.json)。原始实时记录位于 `rvc-golden/realtime-patches-20261008/gpu-v1`。
原始记录与 WAV 已复制回本地，由 Git 忽略；Git 只保存脚本、报告和汇总数据。

## 使用与后续边界

设备端独立启动器为：

```text
/home/moyamryia/mozart-realtime-patches-20261008/gpu-runtime/run-realtime.sh
```

该启动器已通过 `--help` 检查。完整回放通过监督程序使用了相同的 CUDA 参数和独立包目录。
实时麦克风启动示例：

```bash
~/mozart-realtime-patches-20261008/gpu-runtime/run-realtime.sh \
  --reference ~/Mozart/rvc-golden/clone-tts/qiqi-20261004/reference-qiqi-natural-source.wav
```

该示例本轮未执行。麦克风与实体扬声器的设备名仍需符合实际硬件。
若后续继续优化，应先测量状态传输和会话调用开销，再评估半精度或状态常驻 GPU。
本轮没有验证 FP16、CUDA Graph、TensorRT 或完整 Pocket PyTorch 路径。
播放队列仍需按音频时长消费输出；GPU 迁移不会缩短已有音频。

## 失败记录与回退

初次 CUDA 构建遇到未使用注意力代码的编译错误，随后补齐 CPU 卷积共享激活实现。
首次 CUDA 模型加载又因裁剪掉内部复制算子而失败。
补齐算子后，重新编译受影响目标，再运行上述完整对照。
`pocket-gpu-v1` 保留早期失败证据；最终结论只使用 `pocket-gpu-v2` 和 `gpu-v1`。

本轮未修改设备原仓库或原 Python 环境。设备原仓库提交仍为 `fd9a0fc`，原有未提交改动保留。
若要停止使用 GPU，只需使用通用启动器的默认 CPU 参数，并省略 `--tts-pythonpath`。
