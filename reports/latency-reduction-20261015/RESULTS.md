# 延迟优化实测（2026-10-15）

本轮针对[逐功能测量](../feature-latency-20261014/RESULTS.md)中的文件转换、字幕传输、取消和播报等待进行优化。
代码从 `1e4d8e5` 开始修改。测试使用隔离副本，没有覆盖 Jetson 原有 `/home/moyamryia/Mozart` 检出。

## 已验证的变化

| 功能 | 旧结果 | 新结果 | 计时范围与样本 |
| --- | ---: | ---: | --- |
| 8.399 s 文件暖态转换 | 7.025 s | 1.760 s | 上传请求开始→完成状态可见；各 2 次 |
| 文件首次请求 | 12.514 s | 3.085 s | 同上；新进程激活模型后的首次请求，各 1 次 |
| 字幕独立追加试验 | 187.43 ms | 0.43 ms | 文件追加前→Python SSE 客户端读取完整正文；各 100 条 |
| 完整语音链字幕 | 106.65 ms | 4.95 ms | ASR 事件发出→Python SSE 客户端收到正文；各 292 条 |
| 固定文本播报首块就绪 | 913.16 ms | 277.01 ms | 工作进程收到任务→首个音频块返回；4 段文本，各重复 2 次 |
| 完整回放播报首块就绪 | 930.19 ms | 276.38 ms | 合成任务开始→首块就绪；各 41 个任务 |
| 连续译文父字幕首播 | 8.178 s | 7.651 s | 最终 ASR 事件→该父字幕首个音频块播放；各 23 个父字幕 |

文件表使用最终扩大形状范围的配置。后续文件专用配置的内存复测另列于下文。
旧语音回放单独运行。新语音回放同时运行实时 RVC，不能视为严格相同负载的重复试验。
字幕独立试验没有其他业务负载。完整语音链的计时包含桥接程序与客户端接收开销。
所有播放结果使用按音频时长等待的 ALSA `null`。时间戳不包含物理设备输出与浏览器绘制。

## 实现与配置

- 字幕服务使用 Linux `inotify` 文件事件唤醒。文件监听不可用时，服务使用 20 ms 轮询。
- 字幕服务在发送响应头前确定读取起点。服务处理不完整行、文件替换、截断和断开连接。
- 文件推理在 F0、HuBERT、索引和 Generator 阶段之间检查取消标志。
- 播报停止先关闭输入管道。工作进程超过 100 ms 退出等待后，服务终止并回收该进程。
- TensorRT 引擎使用定长读取。性能日志分别记录读取、运行时创建、反序列化和分配耗时。
- 文件配置使用整段 Generator、动态 RMVPE 和动态 HuBERT TensorRT 引擎。
- 早期播报默认使用 3 个潜在帧，每块对应 240 ms。旧配置为 15 个潜在帧，即 1.2 s。
- 下一音频块已经就绪时，播放线程合并相邻块的状态落盘。最终完成状态仍持久化。

PocketTTS 每个潜在帧对应 80 ms 音频。网络契约帧为 20 ms。
早期播报保留 CPU int8、2 个线程、5 步生成和种子 42。
使用 `--tts-audio-chunk-frames 15` 可以恢复旧块大小。独立服务使用 `--audio-chunk-frames`。
普通完整预览和下载任务不使用早期解码块。改变早期解码块会改变 PCM，不能宣称逐样本兼容。

平台为 Jetson Orin Nano，`MAXN_SUPER`，Release，ONNX Runtime 1.29.0 与 TensorRT。
最终原生构建关闭 `USE_CUDA_EP`，启用 `USE_ONNX` 和 `USE_TENSORRT`。
本机 CUDA EP 运行实际模型时出现 `cudaErrorNoKernelImageForDevice`。附着 CUDA provider 不代表成功的 GPU 推理。
测试没有改变电源模式、固定频率或清空磁盘缓存。新进程启动不等于冷磁盘读取。

## 文件正确性与资源成本

固定输入为 `qiqi-espeak.wav`，单声道、16 kHz、134382 个采样，即 8.398875 s。
输入 SHA-256 为 `124e1967e74977cd4156d52ed2718e4e600cad6ea85db37b910e8e95491897bd`。
检查点为七七 RVC v2、F0、48 kHz、202 个 speaker、768 维 HuBERT 特征。测试使用 speaker 0。
参数固定为音高偏移 0、索引率 0、RMS 混合率 1、protect 0.33。文件测试不启用 RNNoise 或 VAD。

验证先使用原始 PyTorch Golden 捕获的输入、mel、F0、特征、离散音高、连续音高和显式噪声。
Python ONNX 比较检查形状、类型、有限值、数值误差和输出时长。
实际 C++ TensorRT 比较通过 12 个形状用例，每个用例预热 1 次、计时 5 次。
Generator 验证长度为 200、398、1038 和 2198。RMVPE 验证长度为 32、128、1056 和 2208。
HuBERT 验证输入为 32000、64000、166382 和 352000 个采样。
Generator 最大相对均方根误差为 0.000194。运行时记录确认实际使用 TensorRT。

旧固定窗口路径的优化前后 PCM 逐样本一致。整段路径输出为 402240 个采样，即 8.38 s，与 Golden 时长相同。
旧窗口拼接输出为 403146 个采样。两条路径的有效长度不同，不能直接宣称波形完全一致。
原始 PyTorch 与 C++ 使用不同随机数生成器。直接比较时，频谱相关系数为 0.98751，有声判断交并比为 0.52423。
补充对照保留原 Golden 特征和音高，只改用与 C++ 相同的显式噪声。
补充对照的频谱相关系数为 0.99999999，F0 中位差与 P90 均为 0 cents，有声判断交并比为 1。
8.399 s 输入的波形相对均方根误差为 0.000921。
20 s 输入补充对照的频谱相关系数为 0.99999992，F0 中位差与 P90 均为 0 cents，有声判断交并比为 1。
该长输入的波形相对均方根误差为 0.002600。两项对照均无满幅采样。
这些结果区分了随机噪声影响与管线数值差异。

原始 Golden、ONNX 和后端 WAV 均保留供并排试听。本轮没有新增人工听感验收。
最终快速文件配置处理后的进程 RSS 为约 4.84 GB，旧固定路径约为 3.06 GB。
形状范围外的 20 s 文件完成了 CPU 回退，耗时 15.380 s，没有截断输入。
回退后再次运行短文件，TensorRT 恢复，输出与回退前逐样本一致。
扩大范围后，同一 20 s 文件使用整段 TensorRT，耗时降至 3.442 s，输出 959040 个采样，输出长度与该输入的 Golden 相同。
30 s 文件仍超出范围。该文件用 CPU F0 和固定窗口完成转换，耗时 22.380 s，输出 1440000 个采样。
再次运行短文件时，TensorRT 恢复，PCM 与回退前一致。该组试验的最低可用内存为 2.09 GiB。
该内存成本不适合直接套用于所有并行业务。文件和实时 RVC 使用独立的资产配置。

文件专用配置省略实时特征资产，处理后的进程 RSS 降至 4.19 GB。
该配置的短文件暖态中位数为 1.767 s，输出 PCM 与同资产的组合配置逐样本一致。
文件专用配置仅用于文件转换。完整语音链与快速文件引擎的同时运行未在本轮验证。

## 取消和引擎加载

旧文件取消从请求到终态为 4.295 s，旧播报停止接口耗时为 3.073 s，各 1 次。
新阶段边界取消的两次结果为 46.06 ms 和 46.60 ms。
新播报停止接口的三次结果为 72.08 ms、72.36 ms 和 72.62 ms。
首条使用 15 个潜在帧；后两条使用 3 个潜在帧。
GPU Generator 运行中取消的请求接受耗时为 1.74 ms，取消终态耗时为 1.079 s。
底层模型调用当前不能被该协作标志抢占。更长的 CPU 回退调用也会增加取消等待。
不能把阶段边界的 46 ms 当作所有输入的取消上限。

有一次模型切换超过旧测试客户端的 15 s 等待。服务日志中的加载间隔约为 31 s。
该异常没有在随后复测中重现，根因尚未确认。新代码增加了分段计时日志。
测试客户端扩大等待时间仅用于收集证据，不能算作延迟优化。

同一 378.64 MB 引擎文件的独立读取试验中，逐字节迭代读取中位数为 1688.53 ms，定长读取为 143.43 ms。
试验交替执行两种方法，每种方法排除首次后各计 3 次，读取内容逐字节一致。
该读取对照支持定长读取的收益，但不能解释此前未复现的 31 s 异常。

## 小块播报与并行稳定性

下表的帧数均指 PocketTTS 潜在帧。

| 配置 | 首块中位数 | 完整生成中位数 | 4 段文本识别与旧配置一致 |
| --- | ---: | ---: | ---: |
| 15 帧、2 线程 | 913.16 ms | 1832.92 ms | 4/4 |
| 10 帧、2 线程 | 646.23 ms | 1856.43 ms | 4/4 |
| 5 帧、2 线程 | 381.85 ms | 1894.41 ms | 3/4 |
| 3 帧、2 线程 | 277.01 ms | 1945.55 ms | 4/4 |
| 5 帧、4 线程 | 367.49 ms | 1873.74 ms | 3/4 |

各行使用相同 4 段英文、同一参考音色、相同种子，每段重复 2 次。
3 帧配置的完整生成中位数增加 112.63 ms，约为 6.1%。
3 帧配置的 log-mel 相关系数最低为 0.99779。
官方 PyTorch RMVPE 比较的 F0 P90 差异最高为 23.09 cents，有声判断交并比最低为 0.9583。
相同采样位置的块边界跳变与旧音频接近。该比较不等于所有文本和音色的质量认证。
最初的简单自相关音高估计存在倍频误判。最终统计使用官方 RMVPE，没有采用该估计结果。

最终 120 s 回放保留完整文本，产生 123.6 s 音频。
首块就绪的 P95 为 510.26 ms，最大为 1248.31 ms，不能保证每个任务均在 300 ms 内就绪。
父字幕首播的 P95 为 10.533 s，最大为 10.776 s。
原文覆盖为 23/23，播报完成为 23/23 个父字幕、41/41 个任务。
所有 41 个结果均等于按顺序连接的音频块，没有满幅采样，播放顺序保持一致。
翻译检查通过 22/23 个父字幕。已有的未译中文问题仍出现在 1 个父字幕中，不能把覆盖完成当作语义正确。
相邻音频块时间戳间隔超过 20 ms 的记录为 2/506，最大为 37.33 ms。
这是管道提交时间戳间隔，不是物理声卡静音间隔。

并行实时 RVC 共处理 512 个块。推理中位数为 65.04 ms，P95 为 103.23 ms，最大为 131.34 ms。
推理错误、迟到块、输入积压、输出积压及稳态欠载均为 0。
20 个启动欠载帧对应原有的 400 ms 软件等待预算。测试未缩短该预算。
该组合运行的最低可用内存为 3.20 GiB。

## 仍然影响延迟的因素

完整播放全部译文时，播放队列仍是主要等待来源。
固定旧回放的提交时间、FIFO 顺序和 123.6 s 音频时长，即使计算与切换耗时为零，父字幕首播等待中位数仍为 6.456 s。
该推导只适用于保存的任务时间线，不能作为其他输入的物理下界。
将全部音频时长缩短到原来的 `1/1.05` 后，推导中位数可降到 2.173 s。
这一结果说明轻微加速有潜力，但它改变播放节奏，且本轮未验证保音高变速的音质。
默认配置保留原速，也没有删除或概括译文。

在线识别断句仍包含 600 ms 静音窗口。缩短该窗口需要另外验证断句与识别错误。
翻译热请求仍约为 0.4 s。小音频块改善首块等待，不能缩短必须播放的全部音频时长。
完整预览仍需等待全部音频就绪。本轮没有缩短其完整生成耗时。
30 s 文件的 CPU 回退仍慢。更大的形状范围或分段 GPU F0 仍有优化空间，需要另行验证内存与 Golden。
当前结果不能证明更换模型、改变播放节奏或调整文本策略后仍不能继续降低延迟。

## 复查入口

- [指标汇总](metrics.json)：包含逐项文件结果、回放统计、GPU 张量比较及取消计时。
- [源码与资产清单](manifest.json)：包含 SHA-256、实际运行路径和构建配置。
- [本机 CTest](host-tests.log)：25/25 个测试目标通过。
- [Jetson CTest](jetson-tests.log)：22/22 个测试目标通过。
- [工具 Python 测试](python-tests.log)：150/150 个用例通过。

Jetson 的最终文件资产位于 `/home/moyamryia/mozart-latency-20261015/profile20/assets/`。
Generator profile 为 100/398/2400，RMVPE 为 32/1056/2304，HuBERT 为 32000/64000/384000。
三组数值依次表示最小、优化和最大形状。测试覆盖上述 12 个用例，未逐一验证范围中的全部长度。
实时分离 Generator 保留原固定契约。这些动态形状只用于文件配置。

独立快速文件配置已完成真实转换复查。三个阶段均使用 TensorRT，PCM 与文件基准逐样本一致。
在 Jetson 上运行：

```bash
/home/moyamryia/mozart-latency-20261015/build/state/mozart_stated \
  /home/moyamryia/mozart-latency-20261015/deployment/backend-file-fast.json
```

该配置使用 HTTP 18080，文件模型 ID 为 `quality`，省略实时特征资产。
本轮未更改原有部署配置，也未留后台测试服务。运行前需释放该配置的端口。
早期播报使用已测试的独立运行时和现有启动流程，默认潜在帧数已改为 3。

原始日志、失败记录、试听 WAV、模型、引擎和张量保留在
`rvc-golden/latency-reduction-20261015/`。大型产物不写入 Git。
新增诊断脚本及形状参数见 [Golden 说明](../../rvc-golden/README.md#file-and-control-latency-diagnostics)。

## 并排试听

以下文件为本机保存的实际产物。

| 输入 | 原始 Golden | 动态 ONNX | 最终后端 |
| --- | --- | --- | --- |
| [8.399 s espeak 输入](/home/moyamryia/Projects/Project-Mozart/rvc-golden/runtime-efficiency-20261014/reference/input/qiqi-espeak.wav) | [原始 PyTorch](/home/moyamryia/Projects/Project-Mozart/rvc-golden/runtime-efficiency-20261014/reference/output/qiqi-espeak-python-reference.wav) | [ONNX](/home/moyamryia/Projects/Project-Mozart/rvc-golden/latency-reduction-20261015/verification/qiqi-espeak-dynamic-onnx-generator.wav) | [后端](/home/moyamryia/Projects/Project-Mozart/rvc-golden/latency-reduction-20261015/deployment/qiqi-espeak-deployment-mozart-backend.wav) |
| [20 s 重复输入](/home/moyamryia/Projects/Project-Mozart/rvc-golden/latency-reduction-20261015/profile20/file-only-final/qiqi-espeak-repeated-20s-input.wav) | [原始 PyTorch](/home/moyamryia/Projects/Project-Mozart/rvc-golden/latency-reduction-20261015/profile20/reference20/qiqi-espeak-repeated-20s-python-reference.wav) | [ONNX](/home/moyamryia/Projects/Project-Mozart/rvc-golden/latency-reduction-20261015/profile20/verification/qiqi-espeak-dynamic-onnx-generator.wav) | [后端](/home/moyamryia/Projects/Project-Mozart/rvc-golden/latency-reduction-20261015/profile20/file-only-final/candidate-fast/qiqi-espeak-candidate-fast-3-mozart-backend.wav) |

同噪声补充对照分别保存在 `native-noise-reference/` 和 `profile20/native-noise20/`。
原始 Golden 保留，未被补充对照替换。

| 固定文本 | 旧 15 帧输出 | 新 3 帧输出 |
| --- | --- | --- |
| Mother told me that during the summer vacation, | [旧输出](/home/moyamryia/Projects/Project-Mozart/rvc-golden/latency-reduction-20261015/speech-blocks/baseline/00-00.wav) | [新输出](/home/moyamryia/Projects/Project-Mozart/rvc-golden/latency-reduction-20261015/speech-blocks/three/00-00.wav) |
| Repay the loan | [旧输出](/home/moyamryia/Projects/Project-Mozart/rvc-golden/latency-reduction-20261015/speech-blocks/baseline/00-03.wav) | [新输出](/home/moyamryia/Projects/Project-Mozart/rvc-golden/latency-reduction-20261015/speech-blocks/three/00-03.wav) |
