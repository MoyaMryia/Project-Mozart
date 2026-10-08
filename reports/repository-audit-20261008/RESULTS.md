# 仓库审计与文档整理

日期：2026-10-08。代码基线：`746e4bd`。本轮修改文档并保存诊断探针，没有修改生产 C/C++、Python 服务或模型资产。
扫描范围为 HTTP 控制面、模式/文件生命周期、模型选择、部署构建、字幕/播报接口、前端构建与维护文档。
这次审查不是模型质量验收，也不证明全部代码没有其他缺陷。

## 主要发现

### P1：未完成 HTTP 请求阻塞控制面与退出（已复现）

代码位置：[`api/src/http_api.cpp`](../../api/src/http_api.cpp)，`run_server()` 第 266 行、`handle_request()` 第 290 行、`stop()` 第 229 行。
普通请求直接在 accept 线程执行。已接受 socket 没有读取超时，`recv()` 等待完整头部/请求体。
`stop()` 只关闭监听 socket，再 join 该线程，不关闭正在读请求的连接。

一个客户端保持不完整请求头时，后续健康、模式与取消请求无法处理；服务停止也要等待该客户端结束。
这不需要并发高负载，普通客户端异常或慢上传即可触发。
保存的 [HTTP 探针](http_probe.cpp) 与 [驱动脚本](probe_http.py) 连接本地 18997 端口，不访问外网或模型。
实测结果：

```json
{
  "second_request_blocked_for_at_least_seconds": 1,
  "health_recovers_after_first_client_closes": true,
  "shutdown_blocked_until_client_closes": true,
  "server_exit_code": 0
}
```

修复方向：请求读取/写入使用绝对截止时间，活动连接可在停止时关闭，处理并发度有明确上限。
验收包含不完整头部、未完成请求体、慢下载、断连和停止；其他控制请求及退出都需要时间上限。
仅设置单次 `recv` 超时仍可能被持续少量发送的数据延长，需覆盖整次请求时限。

### P1：压缩文件没有解码资源上限，取消不能中断重操作（代码确认）

代码位置：[`file_rvc_worker.cpp`](../../rvc-backend/src/state/file_rvc_worker.cpp)，第 52、150、163、172、199、205 行。
FFmpeg 通过同步 `std::system()` 解码/编码；普通路径将全部 f32 音频读入内存，再调用一次完整 `pipeline_.process()`。
取消只在若干步骤和可选 RNNoise 帧循环检查，不能中断 FFmpeg 或正在执行的推理。
控制器退出等待文件线程结束。

上传压缩文件的字节上限不限制播放时长。很小的压缩文件也可以解码出很长音频，随后占用磁盘、PCM 内存和模型中间张量。
文档过去写“帧边界取消”，已修正。已有取消测试确认状态保护，未证明重操作可中断或取消时延有界。
本轮没有制造 OOM 或运行无界解码。

修复方向：解码时长/样本数硬上限、可终止且可等待的受控子进程、超时、取消及分块内存预算。
验收：长压缩输入和异常解码不会耗尽资源；取消/退出有时限；未完成结果不发布为成功。
路径目前只用双引号包裹后传入 shell，还应随子进程改造验证带空格、引号和 shell 特殊字符的合法目录。

### P1：生产 ONNX 依赖缺失可以生成“loaded”但不能推理的部署（已复现 stub 行为）

代码位置：[`rvc-backend/CMakeLists.txt`](../../rvc-backend/CMakeLists.txt) 第 103–108 行；
[`onnx_engine.cpp`](../../rvc-backend/src/rvc/onnx_engine.cpp) 第 234–260 行。
CMake 找不到 ONNX Runtime 时会把 ONNX 关闭。stub 的 `load()` 只判断路径存在，`run()` 才抛出运行时不可用异常。
因此构建成功、管线为 real 或模型 loaded 都不能证明真实 ONNX 已就绪。

[资产探针](asset_probe.cpp) 使用刻意无效的临时 `.onnx` 文件，在 `USE_ONNX=OFF` 的主机运行时中得到：

```json
{
  "factory_skips_later_loadable_model": true,
  "stub_accepts_invalid_onnx_file": true,
  "stub_execution_throws": true
}
```

这只验证 stub 和初始化行为，不是任何真实模型的 ONNX 正确性证据。
修复方向：显式区分测试构建与生产能力；生产请求 ONNX 时缺少依赖应失败，状态不得把 stub 当作真实可推理资产。
保留纯 TensorRT 部署需要单独明确的配置与能力验证。

### P1：初始模型加载失败后不尝试后续模型（已复现）

代码位置：[`pipeline.cpp`](../../rvc-backend/src/rvc/pipeline.cpp) 第 234–243 行。
工厂按目录顺序寻找 `exists == true` 的模型，但忽略 `load_model()` 返回值并无条件 `break`。
[`model_loader.cpp`](../../rvc-backend/src/rvc/model_loader.cpp) 第 104 行附近将 `.pth` 也视为 exists，而运行时不支持 libtorch 回退。

探针先按实际枚举顺序给首目录放置 `.pth`，给后目录放置可被主机 stub 加载的文件。
工厂初始模型为空，显式加载后目录则成功，证明未继续搜索。
同样的 false 返回路径也适用于配置无效或其他资产加载失败；本轮没有使用真实神经模型验证这些分支。

修复方向：显式初始模型设置；仅成功加载后结束搜索；加载失败保留可观察原因。
验收：目录顺序、不可加载模型和 split-only 模型都不导致不确定启动或错误模式选择。

## 性能补充核对

本节依据 2026-10-06/07 保存的实测报告补充，没有启动新的模型推理或板上性能测试。
另已分页核对聊天 `Review Mozart PR and Jetson setup` 的全部可读记录，直至首轮 PR 审查。
Session ID：`01a0f9ef-594b-71a0-b2b1-521c79179f27`。
上一版把旧配置瓶颈、已修复机制与待验收能力混在一起；以下为核对后的状态。

### 已修复机制与最新验证

| 已修复问题 | 当前实现与验证 |
| --- | --- |
| 整句生成后才能首播 | 短片段合成与播放重叠；`long-v5` 有 82 个任务在父任务生成完成前开始播放 |
| 播放任务占满四个合成名额 | 实时任务采用 24 s 估算音频预算和 12 s 实际播放缓冲；下载任务保留独立容量限制 |
| 缓冲满后丢弃成功合成音频 | 等待容量；最终 `long-v5` 没有成功合成后丢弃或未播放的片段 |
| 临时容量不足与截止时间估算错误 | 最多四项延后文本 FIFO；首播估算计入前方延后文本，在合成前拒绝预计超时任务 |
| 首任务承担引擎加载 | 启动器预加载引擎，在采集前确认运行时就绪 |
| Pocket 原生帧数限制无效、已复现的异常重复 | 完整设置 options，经固定文本控制修正标点和片段边界；保留原词、数字及顺序 |
| 连续 RVC 被字幕分段重置 | 2026-10-05 polish 已修复；真实帧/时间不连续仍触发重置 |

代码依据为 `tools/speech_service.py`、`tools/run_translated_speech.py`、`tools/clone_worker.py`、
`tools/speech_chunks.py` 与 `rvc-backend/src/rvc/streaming_pipeline.cpp`。
现有回归包含延后文本首播估算、容量等待、下载首音截止条件和原生 options 复制行为。

最新 `sentence-context short-v2` 及后续 mock 演示均完成 18/18 个已接收任务，零容量拒绝、失败、到期或采集过载。
另有六个源字幕因数字、极性或边界不确定而未播报，全部源字幕覆盖为 18/24。
这支持当前受控演示，不能以减少工作量证明推理提速。
Session 后续已记录用户完成物理检查，且全部 24 段中文源文已获用户确认；本轮无需再要求重复确认。

### 历史测量与后续验收

| 项目 | 保存的证据 | 仍需处理的问题 |
| --- | --- | --- |
| 首音延迟 | 旧 30 分钟 `long-v5`：提交至首播 median 11.78 s、max 22.61 s；230 项配对 p95 从 25.63 s 降至 18.26 s | 后续新增截止时间、延后队列与预热修复；当前配置需重新测量，不能沿用旧值作为现状 |
| 持续吞吐 | `long-v5`：365/379 有效任务完成，14 个容量拒绝，448 个英文词未播报 | 最新短测零容量拒绝；最终配置仍需长测，不能断言旧拒绝数仍适用 |
| 播放需求 | 早期固定 120 s 片段生成 132.56 s 音频 | 该片段即使立即生成，正常速度播放也会积压；不是全部输入都固定多 10.5% |
| TTS CPU | 两线程 RTF 0.724、四线程 0.690，六项输出相同 | 四线程仅缩短约 4.7% 计算时间；还需全链竞争测试 |
| TTS GPU | 已检查的 sherpa 运行时请求 CUDA 后提示回退 CPU；当前 worker 显式使用 CPU | 可选优化，尚无 GPU 加速实测；不是当前演示的必要条件 |
| 4B 与共存 | 120 s 候选测试最低可用内存约 1.57–1.60 GiB，有 swap-in；这些播报测试关闭 RVC | 旧 0.8B 配置已有四次 45 s 全组件共存测试，最低约 1.99 GiB；当前播报服务与最终模型长期共存仍待验收 |

最终 30 分钟测试的 CPU 中位数为 51.1%，p95 为 60.5%，没有显示持续总 CPU 饱和。
这不表示生成没有性能成本，也不能排除局部串行瓶颈、内存压力和播放时间限制。
后续 4B 短测增加了不确定性拦截，输入到 TTS 的工作量减少，不能据此宣称推理提速。

当前主要后续工作是源语义修复，以及修复后最终配置的 30 分钟准确性、队列和内存验收。
最新短测 ASR final 至首播 median 12.31 s、max 27.41 s，表明等待仍存在；文本与工作量已变化，不能直接比较提速。
旧全组件测试中的 TTS 生成结果保存为文件，未进入实时物理播报；不能据此宣称当前配置长期共存已验收。
GPU 迁移与加入实时 RVC 的共存压测列为独立优化/扩展验收，不作为受控播报演示完成的必要条件。

来源：[分片最终长测](../chunked-speech-20261006/RESULTS.md#final-long-v5-outcome-and-verified-collection)、
[性能拆分](../overload-diagnosis-20261006/PERFORMANCE.md)、
[GPU 检查](../overload-diagnosis-20261006/GPU.md)、
[候选模型短测](../sentence-context-20261007/RESULTS.md#complete-collection-quality-limits-and-final-decision)、
[后续可靠性修复](../reliability-20261006/RESULTS.md)、[mock 演示](../demo-20261007/RESULTS.md)、
[旧配置共存测试](../full-run-20261005/RESULTS.md)。

## 其他缺口

| 优先级 | 问题 | 证据与影响 |
| --- | --- | --- |
| P1 | 模型缓存无上限、`IDLE` 不卸载全部引擎 | `ModelManager::models_` 保留对象；特征初始化可能加载两套资产。旧文档的空闲释放保证已删除。 |
| P1 | 动态 ONNX 实际 GPU/CPU 回退不完整可见 | 历史板测存在 sm87 kernel 问题；当前 `model_info()` 未完整显示实际资产与回退。需要部署主机复测。 |
| P2 | SSE 线程与断连回收无连接上限 | 每连接一线程，发送数据时才发现断连，空闲无心跳；本轮为代码确认，未做大连接压力测试。 |
| P2 | 文件终态历史无数量上限 | `jobs_` 入队只限制未完成数；终态仍进入 `/api/status` 序列化，长期自动任务可增加内存与响应时间。 |
| P2 | CI 只覆盖主要原生路径 | `.github/workflows/native-tests.yml` 未运行本轮的 Python、前端构建或原生 HTTP 连接生命周期探针。 |
| P2 | 上传上限文字与代码不一致 | 请求读取上限约 110 MiB，但错误文本写 100 MB；整个 multipart 请求与文件数据也没有分别定义上限。 |
| P2 | 语义、快速输入覆盖和声线仍未验收 | 见 2026-10-06/07 报告；有限数字、极性与边界规则不能替代源文和试听。 |
| P3 | `.index`、PipeWire、Zero-Shot VC | 生产检索停用、PipeWire 占位、两个 VC 模式未实现；参考 TTS 已独立实现。 |

字幕 JSONL 的替换/截断重开已经实现，旧 TODO 已更正。
SSE 初次连接从文件末尾开始，不重放断连期间的字幕；是否需要补发仍是产品决策。

## 文档修改

整理 11 份当前说明：根 README、DESIGN、TARGET、TODO，state README/API，中英文部署，参考播报，Golden README，USB 决策。
新增 [编写规则与术语表](../../docs/WRITING.md)。
英文使用 ASD-STE100 Issue 9 的写作规则和受控词义原则，核对了官方规则与部分通用词条。
句长检查通过不等于完整词表、词性和语义符合性审核；本轮不声明认证或全部符合性已获独立确认。
中文采用短句、统一术语、条件前置和明确验证范围，参考使用说明、标点与标准化文件相关国家标准。
未确认与 STE 受控词表直接对应的通用中文标准，不将这些国标称为中文版 STE。

修正的事实包括：

- 构建和启动统一到 `build-gpu/`；独立后端构建不产生守护进程。
- `IDLE` 仍可保留引擎；模式切换不停止外部 ALSA 麦克风。
- 删除未实现的 CUDA 抢占、显存清理和 PipeWire 物理设备保证。
- protect 位于 Generator 前，混合特征；不是最终波形混合。
- 普通静态、full-length/dynamic 和 split realtime 资产分别说明，不把固定 T=200 写成架构永久限制。
- 健康检查只代表存活；`device: cuda`、stub loaded 和历史组件耗时都不能证明当前 GPU/整机能力。
- 取消时延、缓存软目标、输出 UDP 分片和模型选择限制明确写出。
- 文件 API 使用 `model_id`，记录 `speaker_id` 为兼容别名。

31 份历史报告、人工源文和 USB 实验文档的正文保持不变，只增加历史记录说明。
旧架构 HTML 增加显眼的历史设计提示。报告索引补全缺失报告。

## 验证

| 检查 | 本轮结果 |
| --- | --- |
| 重新配置并构建 CPU 原生运行时 | 通过；ONNX/TensorRT 关闭 |
| CTest | 15/15 通过 |
| Python unittest | 85/85 通过 |
| 前端类型检查与 Vite 生产构建 | 通过 |
| HTTP 健康阻塞与停止探针 | 两个问题复现；客户端关闭后恢复/退出 |
| 初始模型与 ONNX stub 探针 | 两个问题复现 |
| 文档内部链接、代码块与 diff 空白检查 | 通过；历史引用的本地实验产物不作为当前部署依赖 |
| 当前英文指南句长与段落 | 四份指南均通过 25 词/句、6 句/段结构检查；编号操作另检 20 词/句 |
| Generator 比较工具 self-test | 未运行成功：系统 Python 缺少 `onnxruntime`，记录的 Golden Python 路径也不存在 |
| Golden、ONNX 数值对比、Jetson 性能、物理试听 | 本轮未运行；没有修改相关资产或推理实现 |

初次网络测试因沙箱不允许创建 socket 失败；允许本地回环访问后通过，未将权限错误计作产品缺陷。

## 探针复现

先完成 README 中的主机原生构建。以下链接库路径对应本轮 FetchContent 的主机构建；
若使用系统 spdlog/json，需按实际编译配置调整头文件和库路径。

```bash
g++ -std=c++17 \
  -Iapi/include -Irvc-backend/include -Imonitor/include -IIO/include \
  -Ibuild/host/_deps/json-src/include -Ibuild/host/_deps/spdlog-src/include \
  reports/repository-audit-20261008/http_probe.cpp \
  rvc-backend/src/state/log_store.cpp \
  build/host/mozart_api/libmozart_api.a \
  build/host/mozart_api/mozart_monitor/libmozart_monitor.a \
  build/host/_deps/spdlog-build/libspdlogd.a -pthread \
  -o /tmp/mozart-http-probe
python3 reports/repository-audit-20261008/probe_http.py /tmp/mozart-http-probe

g++ -std=c++17 -Irvc-backend/include -IIO/include \
  -Ibuild/host/_deps/json-src/include -Ibuild/host/_deps/spdlog-src/include \
  reports/repository-audit-20261008/asset_probe.cpp \
  build/host/libmozart_rvc_runtime.a \
  build/host/_deps/yaml-cpp-build/libyaml-cppd.a \
  build/host/mozart_io/libmozart_io.a \
  build/host/_deps/spdlog-build/libspdlogd.a \
  build/host/mozart_pre/libmozart_pre_core.a \
  build/host/mozart_pre/librnnoise.a -lasound -lm -pthread \
  -o /tmp/mozart-asset-probe
/tmp/mozart-asset-probe
```

HTTP 探针只绑定 loopback，默认端口 18997，可通过第二个脚本参数修改。
资产探针必须链接 `USE_ONNX=OFF`、`USE_TENSORRT=OFF` 的主机运行时；它刻意检查 stub，不能用于真实资产验收。
