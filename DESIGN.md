# Mozart 系统设计

本文说明当前实现。代码、接口和测试共同提供事实依据；测量值只适用于对应报告中的配置。
产品目标见 [TARGET.md](TARGET.md)，剩余工作见 [TODO.md](TODO.md)。

## 1. 部署结构

目标平台为 NVIDIA Jetson Orin Nano Super 8GB。CPU 与 GPU 共享内存，资源预算必须包含所有常驻进程。
RVC 使用 C++17 与 ONNX Runtime/TensorRT；预处理使用 C11；字幕与参考音色播报使用 Python/sherpa-onnx。
模型导出和 Golden 参考使用独立 PyTorch 环境。

```text
mozart-pre (ALSA / WAV)
  -> UDP input -> mozart_stated
                   -> RealtimeRvcWorker -> AudioWorker -> RVC -> UDP output
                   -> FileRvcWorker -> FFmpeg -> RVC -> WAV
  -> UDP copy -> STT -> translator -> subtitle JSONL -> HTTP SSE
                                 -> speech service -> ALSA
```

RVC 后端入口为 `state/src/main.cpp`。`StateManagerDaemon` 读取配置并创建 `ModeController`、管线和 HTTP 服务。
`ModeController` 管理生命周期、模式和队列，不处理 PCM 样本。
字幕与播报使用 `tools/run_translated_speech.py` 监督子进程；原生守护进程代理播报 API。

| 组件 | 当前职责与限制 |
| --- | --- |
| `IO/` | UDP、Mock、SPSC 环；PipeWire 捕获填静音、播放丢弃样本 |
| `preprocessor/` | ALSA 采集播放、WAV 定速回放、HPF、RNNoise、降采样、VAD |
| `rvc-backend/` | 特征提取、合成、分块、模型与文件队列 |
| `state/` | 后端组合入口与退出顺序 |
| `api/` | 原生 socket HTTP、字幕 SSE、播报服务代理 |
| `monitor/` | 系统遥测 |
| `frontend/` | Vue 3 控制界面 |
| `tools/` | 导出、ASR、翻译、播报、启动和诊断脚本 |

## 2. 音频契约

唯一结构定义为 [`IO/include/mozart/frame_meta.h`](IO/include/mozart/frame_meta.h)。
所有契约帧使用单声道 float32 PCM，帧长 20 ms。

| 帧 | 采样率 | 样本数 | 元数据与 PCM 字节数 |
| --- | --- | --- | --- |
| raw | 48 kHz | 960 | 3856 |
| input | 16 kHz | 320 | 1296 |
| output | 48 kHz | 960 | 3856 |

```c
#pragma pack(push, 1)
typedef struct {
    uint64_t pts_ns;
    uint32_t frame_idx;
    uint8_t  vad_flag;
    uint8_t  energy_db;
    uint8_t  conf;
    uint8_t  segment_id;
} mozart_frame_meta_t;
#pragma pack(pop)
```

元数据共 16 字节，包含时间戳、帧序号、语音标记、能量、置信度和段编号。
预处理维护 VAD 和段信息。序号与时钟缺口会触发流状态重置；段编号变化本身不会触发当前 `StreamingRvc` 重置。
ASR 不应因短暂 VAD 切换而丢弃连续输入。

### UDP 格式

```text
0:  magic       uint32 = 0x4D5A5254
4:  pts_ns      uint64
12: frame_idx   uint32
16: vad_flag, energy_db, conf, segment_id
20: samples     float32[]
```

输入包为 1300 字节，输出包为 3860 字节。
输入包在常见 1500 字节 MTU 下无需 IP 分片；输出包需要分片，不能保证无分片丢包。
默认音频端口为 18000。回包地址由 UDP 流记录的客户端决定；不要让多个测试客户端竞争同一流。

### I/O 与缓冲

`AudioStream` 提供流抽象；C ABI 位于 `mozart/audio_io.h`。
创建、打开、读写、关闭和销毁是独立操作。实时工作单元持有流句柄，控制器管理其生命周期。

SPSC 环使用预分配存储、原子索引和分离缓存行。push/pop 不动态分配内存。
环满时返回失败；上层按路径记录溢出或丢弃旧输入。输出不足时补零，仍需统计欠载和试听连续性。
物理音频当前经外部 ALSA 客户端传输；`OfflineAudioStream` 抽象尚未实现，文件工作线程直接使用 FFmpeg。

## 3. RVC 管线

### 普通文件与 quality 路径

```text
16 kHz PCM -> 归一化与高通 -> RMVPE F0、HuBERT 特征
           -> 可选索引混合 -> 特征插值与 protect -> Generator
           -> RMS 包络混合 -> 重采样 -> 限峰
```

protect 在 Generator 前混合特征，主要作用于无声带振动帧，不混合最终波形与原声。
当前 `.index` 预加载已停用，因此索引混合通常不参与推理。
`rms_mix_rate` 控制后续包络混合；它与 protect 是不同操作。

RVC v2 使用末层 768 维 HuBERT 特征。RMVPE 输入布局为 `[batch, mel_bin, time]`。
特征提取实现包括 radix-2 FFT、HTK mel 滤波器和 Slaney 归一化；正确性由捕获张量对比验证。
F0 方法仅实现 `rmvpe`。API 拒绝 `harvest` / `pm`；旧配置在初始化时改为 `rmvpe` 并记录警告。

静态 legacy Generator 常用 `T=200`，部分 Golden 对齐资产使用其他固定长度。
代码另有 full-length 和动态输入分支，资产可用性必须按具体输入契约验证。
动态轴声明必须通过多个序列长度推理，不能只看文件名。
长文件可能进入整段特征提取与整段 Generator；当前没有解码时长或峰值内存硬上限。

### 低延迟实时路径

`AudioWorker` 使用独立推理线程，聚合 240 ms 块，保留 2.5 s 滚动历史上下文。
该路径使用音高缓存、split Generator 和 SOLA 拼接。
历史上下文来自已收到的输入，不增加 2.5 s 前视等待。

已验证的 `qiqi-zh-realtime` 配置需要以下固定 TensorRT 资产：

| 资产 | 关键形状 |
| --- | --- |
| realtime HuBERT | 输入 `[1,44800]` |
| realtime RMVPE | 输入 `[1,128,32]` |
| Generator front | feats `[1,280,768]`，z `[1,192,30]` |
| Generator decoder | z `[1,192,30]`，audio `[1,1,14400]` |

quality 与 realtime 各有特征资产配置，不可互换固定输入引擎。
缺少实时资产的普通模型可进入 quality/legacy 流式路径。
split-only 模型缺少完整实时资产时不视为部署成功，也不能用于普通文件转换。

已记录首帧出声约 320 ms、稳态块耗时中位数 88 ms、p95 93 ms。
原始证据见 [Golden 参考](rvc-golden/README.md)。其他模型和全部并发场景需要重新测量。

### 模型与运行时

普通目录为 `models/<id>/{<id>.onnx, config.json}`，可包含同名 `.engine` 和 `.index`。
split 资产使用 `<id>-front` 与 `<id>-decoder` 文件名。
`config.json` 提供采样率、特征维度、说话人 ID 和 F0 标志。
检查点版本、F0 支持、说话人数和特征维度仍需在导出前核对。

同路径 `.engine` 加载成功时优先 TensorRT，否则尝试 ONNX Runtime。
CUDA EP 需要 CUDA 版 ONNX Runtime 和 `USE_CUDA_EP=ON`。
`rvc.device: cuda` 与“请求 CUDA EP”日志都不能证明所有计算实际使用 GPU。
历史 sm87 动态 ONNX 测试存在 CUDA kernel 不匹配后 CPU 回退，需按部署资产复测。

工厂按目录扫描选择首个 `exists` 模型，尚无初始模型配置。
当前循环未依据 `load_model()` 返回值继续尝试后续模型；一个存在但不可加载的模型可能中止初始选择。
启动时应显式激活目标模型，并检查状态与实际加载日志。
模型管理器保留已加载对象；尚无缓存容量上限。进入 `IDLE` 不释放全部资产。

真实管线推理失败会抛出异常，不会自动返回 mock 原声。
流式处理在块异常时补静音并记录错误；文件任务返回失败。
组件 mock 只用于诊断，真实部署必须关闭。

## 4. 状态与文件任务

| 状态 | 资源与工作行为 |
| --- | --- |
| `IDLE` | 实时流停止，文件消费停止；模型仍可常驻 |
| `RT_RVC` | UDP 实时处理；文件队列不消费 |
| `FILE_RVC` | 实时流关闭；单消费者处理文件任务 |
| 两个 Zero-Shot VC 状态 | 未实现，HTTP 501 |

模型切换前先停止实时线程。文件任务活跃时，切出文件模式的请求写入待切换槽。
后续请求可以覆盖待切换槽。活动文件任务中的模型变更会返回忙碌。
控制器不会关闭外部 `mozart-pre` 的麦克风，也没有跨算法显存回收流程。

当前实现依靠模式互斥避免实时与文件推理竞争。
尚无按这两个模式分配 CUDA 流优先级的机制，也不保证实时硬件抢占。

文件队列默认深度为 50，计算 `queued`、`processing` 和 `cancelling` 任务。
终态任务不占队列容量，但仍留在内存历史中，直到清理或进程退出。
文件请求上限见 API 文档；压缩文件大小不限制解码时长。

FFmpeg 通过同步 `std::system()` 解码与编码，RVC 推理接收整段音频。
取消标志在若干步骤及可选 RNNoise 帧循环中读取，不能中断 FFmpeg 或正在执行的整段推理。
`cancelling` 仍持有工作单元和管线；退出可能等待其结束。

输出先写 `.part`，成功后重命名。缓存超过高水位时，任务结束清理向 80% 目标执行。
未完成任务的文件受到保护。该目标不是磁盘硬配额；队列与任务状态不跨重启保存。

## 5. HTTP 与配置

当前接口以 [state/API.md](state/API.md) 为准。默认 HTTP 端口为 18080。
`/api/health` 只返回存活状态；模型和队列信息位于 `/api/status`。
字幕 SSE 读取外部 JSONL，并在文件替换或截断后重开；不提供历史事件重放。
参考播报请求代理到本机端口 18081，服务缺失时返回 503。

HTTP 普通请求在一个 accept 线程中串行处理。已接受连接没有请求读取超时。
不完整请求可以阻塞后续控制请求，停止时也可能卡在 join。
SSE 每个连接占一个线程，目前没有连接数量上限。详见 [最新审计](reports/repository-audit-20261008/RESULTS.md)。

配置由 `state/src/daemon.cpp` 读取。文件路径相对于配置文件目录解析。
常用配置分组如下：

| 分组 | 作用 |
| --- | --- |
| `rvc.enabled` | 关闭 RVC 能力与引擎加载，供仅播报部署使用 |
| `rvc.models_dir`、`hubert_path`、`rmvpe_path` | 普通 RVC 资产 |
| `rvc.realtime_hubert_path`、`realtime_rmvpe_path` | 实时固定形状特征资产 |
| `rvc.mock.*` | 组件诊断开关 |
| `rvc.f0_method`、`pitch_shift`、`index_rate`、`filter_radius`、`rms_mix_rate`、`protect` | 推理参数 |
| `network.audio.*`、`network.control.*` | 监听地址、端口与帧设置 |
| `storage.*` | FFmpeg、RNNoise、队列容量、缓存和预设文件 |

帧的固定格式由编译期契约决定。运行时配置不能任意改变帧结构。
完整默认值见 [`rvc-backend/config.yaml`](rvc-backend/config.yaml)。

## 6. 字幕与参考音色播报

Zipformer 消费连续预处理输入，以识别器 endpoint 输出句子。可选 SenseVoice 对句末 PCM 再识别。
Qwen 默认使用 0.8B 模型；其他翻译模型仍需完整的质量和内存验证。
部分数字、可能/不可能、人称和已复现边界形式有专门检查，失败时保留原文并停止该句播报。
检查覆盖有限，识别器一致也不证明源文正确。

参考 PocketTTS 合成翻译文本，不属于 Zero-Shot VC 模式。
服务保存参考配置和结果，逐片段生成完整 WAV，同时使用一个 ALSA 播放单元。
音频预算、截止时间、延迟文本队列和取消行为见 [参考播报契约](tools/REFERENCE_SPEECH.md)。
持续快速输入仍可能产生拒绝或到期句子；这些句子应计入未播报覆盖，不能计作成功吞吐。

## 7. 验证与部署

主机回归检查控制器、DSP、流状态、数字检查和队列逻辑，不证明模型音质。
RVC 调试按 [AGENTS.md](AGENTS.md) 执行：固定 espeak 输入、Golden、捕获张量、ONNX 对比、后端、逐组件定位。
资产与可复用证据放在 `rvc-golden/`；项目审计放在 `reports/`。

根构建与运行命令见 [README.md](README.md#构建)。
生产部署必须确认运行时、目标模型、资产哈希、实际设备、声线和整机内存。
USB 输出采用外接 USB 声卡与模拟音频线；gadget 路线已关闭，见 [决策](usb-gadget/DECISION.md)。
