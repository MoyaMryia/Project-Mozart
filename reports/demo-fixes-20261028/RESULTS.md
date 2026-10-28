# 展示前功能故障修复回归

本轮修复 8 项已复现的功能故障，并处理审查确认的残余缺陷。
实时入口的采集双发、字幕显示、翻译来源和模型参数保持原样。

## 修复结果

| 项 | 修复行为 | 回归入口 |
| --- | --- | --- |
| Windows 麦克风无声 | 按原始字节缓冲读取；回调缓冲按协议帧长分帧；独立线程发送 USB | `test_usb_audio_windows.CaptureTests` |
| USB 排空竞争终止搬运线程 | 将查询后管道已空的 EAGAIN 作为排空结束；其他排空故障记录原因并退出服务 | `test_usb_audio_daemons.SpeakerDrainTests` |
| 单句合成拒绝终止整栈 | 仅已知时长和帧上限拒绝带 `rejected: true`；当前任务失败，下一任务复用原合成进程 | `test_clone_worker`、`test_speech_runtime`、`test_early_audio` |
| 播放线程失败后误报就绪 | 暴露线程存活、存储故障和组件错误；失败已生成待播任务，拒绝新任务并回收进程 | `test_speech_runtime`、`test_early_audio` |
| 文件变声截短仍报告完成 | 检查完整输出样本数和有限值；拒绝截短、空结果和非有限值；失败结果不发布 | `state_file_output_validation` |
| 损坏预设被覆盖 | 读错误阻止保存与删除；写入和发布失败保留原文件；首次保存时创建文件 | `state_presets_*`、`api/tests/test_http.py` |
| Windows 断线返回成功 | 共享停止和错误状态；双工故障停止两端，返回非零退出码 | `test_usb_audio_windows.MainTests` |
| Windows 停止录音后继续写 WAV | 等待音频线程退出后关闭 WAV；关闭一项资源失败时继续回收其他资源 | `test_usb_audio_windows.MainTests`、`SinkTests` |

## 额外收尾

- Windows 音频回调使用有界发送队列。队列满时记录丢弃数量。
- Windows 采集流意外结束或音频线程异常退出时，会话报告失败。
- Windows 接收端对非法帧统一计数。非 PCM 帧也进入 `rejected` 统计，与帧长非法共用一条提示。
- 播报取消或业务拒绝关闭播放管道后，写入线程按任务终态结束，保留健康合成进程。
- 未标记的引擎错误、活动任务的播放管道错误和任务存储失败保留失败状态。
- coverage 策略下，SQLite 成功而 JSON 快照失败时，任务继续运行并提供 `storage_warning`。
- coverage 等待启动的磁盘任务保留 `pending` 状态，供重启后恢复。
- realtime 内存等待任务和已生成待播任务在组件致命故障后结束为失败。
- `POST /api/speech/stop` 按任务隔离存储错误。一个任务写入失败时，继续取消其余任务，返回 `cancel_errors` 计数。
- 清理旧任务时，结果文件删除失败保留任务记录，下次清理重试。该故障写入 `storage_warning`，不停止接收新任务。
- 预设文件缺失时直接按空数组读取。删除冗余的 `status_error` 操作数。
- `tools/export_generator_onnx.py` 提取 `load_inference_weights`。加载前拒绝同一模块的明文与归一化权重混用，加载后移除权重归一化。装有 PyTorch 的机器上，5 项检查点用例现在实际执行。

同时补齐预设路由的成功与 404 状态码映射。依赖说明改为 `RawInputStream` 的 `finished_callback` 要求。

## Jetson 实机复核

- 配置：Jetson Orin Nano Super 8 GB，提交 `75154b6`，`~/Mozart/.venv`（Python 3.12），qiqi-zh，音高偏移 11，索引比例 0.5，nprobe 16。
- 构建：`build-gpu` 的 50 个目标与 `build/usb-pre` 重新编译，无错误。
- 测试：`unittest discover` 520 项全部通过。`test_http.py` 27/27、`test_control.py` 6/6、`state_*` 6 项通过。
- 启动：`tools/run-realtime-voice.sh start`。后端 `rt_rvc`，字幕 `capturing`，翻译就绪。
- 运行数据：音频包 5590/5590，`errors=0`，`late=0`，`send_errors=0`。
- 延迟：`latency.avg_ms` 20.20，`max_ms` 22.69，样本 5598。
- 播放欠载：启动期 20 次，之后不增长。
- 识别：采集 5402 包，`finals=0`。本轮无人说话，未产生字幕和译文。
- 依赖：`.venv` 补装 `pyusb==1.3.1`，供宿主机侧测试导入。

## 检查结果

检查命令统一使用 `tools/ci-local.sh`。HTTP 子测试另行运行 `api/tests/test_http.py`。

| 阶段 | 结果 |
| --- | --- |
| Python | 共 520 项；通过 515 项，跳过 5 项 |
| native | 39/39 通过 |
| HTTP 子测试 | 27/27 通过 |
| state 重复检查 | 20 项各运行 10 次，全部通过 |
| tempo | 6/6 通过 |
| frontend | 29/29 通过；类型检查和生产构建通过 |
| writing lint | 通过。宽泛捕获基线从 6 处缩减至 4 处，涉及两个播报文件 |

故障测试使用真实管道、真实子进程和 FFmpeg。模型及声卡由确定性替身代替。
文件测试检查输出 WAV 的数据样本数。超出输入时长的固定窗口结果仍按既有语义裁剪。

完整日志保存在本机目录：

`/home/moyamryia/Projects/Project-Mozart-backups/demo-fixes-validation/`

## 子代理审查处置

审查检查了重复校验、测试独有守卫、重复阈值、异常处理、验收条件及文档未解决项。

- 修复 Windows 异常退出误报成功、变长回调分帧和资源关闭顺序。
- 修复播报拒绝与关闭管道的竞争、其他待播任务无法结束，以及提交和取消时的 SQLite 故障。
- 删除重复的内部文件描述符检查、互斥的拒绝守卫和冗余的播放任务清理分支。
- 修复预设 HTTP 成功分支的验收：夹具载荷改为真实 `ModeController` 形状，避免断言自造字段。
- 收窄 `/api/speech/stop` 的异常捕获：HTTP 507 与写盘 `OSError` 计入 `stop_errors`。队列读失败先记录原因再停止。
- 合并 `storage_warning` 的两处写入定义，并避免同一删除故障重复打印。
- 保留两端已有的排空封装，避免在本轮引入跨进程架构调整。两端执行相同的故障回归。
- 保留 FIONREAD 失败后记录原因并按空水位处理的既有策略。管道的实际打开和读取失败则触发服务退出。
- 保留固定窗口输出的上限裁剪；新增独立的 WAV 样本数断言验证裁剪结果。
- 保留 coverage 的磁盘等待任务，维持重启恢复语义；组件状态明确记录停止原因。


## 适用范围

配置：main 分支本报告所在提交，父提交 `1194edc`；x86_64；Python 3.10；native Debug；ONNX 与 TensorRT 关闭。
样本数及统计范围见“检查结果”。实时推理算法和模型资产保持原样。
未验证项：Windows 驱动与声卡、真实模型播报和整链彩排。阻塞：Windows 真机未接入，本轮无人对麦克风说话。解除条件：接入 Windows 主机并执行双工录放；在设备旁连续说话 10 分钟，核对中文字幕、英文译文和变声音频。

延迟重构：结果文件名拼接仍分散在生产者和清理逻辑。阻塞：本轮以展示稳定性优先，改动跨 4 处调用。解除条件：单独提交集中定义并重跑本报告全部阶段。
未验证项：结果文件删除持续失败时的历史上限。阻塞：删除失败会保留任务记录，历史条目可超过 `history_limit`。解除条件：在只读挂载的结果目录上运行长时任务并统计条目增长。
跳过项：5 项检查点测试缺少 PyTorch。解除条件：在安装 PyTorch 的环境运行同一 Python 检查阶段。
PR #10、#11、#12 的真实模型验收仍按各 PR 的适用范围执行。
