# 展示前功能故障修复回归

本轮修复 8 项已复现的功能故障，并增加异常路径回归。
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
- 播报取消或业务拒绝关闭播放管道后，写入线程按任务终态结束，保留健康合成进程。
- 未标记的引擎错误、活动任务的播放管道错误和任务存储失败保留失败状态。
- coverage 策略下，SQLite 成功而 JSON 快照失败时，任务继续运行并提供 `storage_warning`。
- coverage 等待启动的磁盘任务保留 `pending` 状态，供重启后恢复。
- realtime 内存等待任务和已生成待播任务在组件致命故障后结束为失败。

## 检查结果

检查命令统一使用 `tools/ci-local.sh`。

| 阶段 | 结果 |
| --- | --- |
| Python | 共 517 项；通过 512 项，跳过 5 项 |
| native | 39/39 通过 |
| state 重复检查 | 20 项各运行 10 次，全部通过 |
| tempo | 6/6 通过 |
| frontend | 29/29 通过；类型检查和生产构建通过 |
| writing lint | 通过；宽泛捕获基线从 6 处缩减至 4 处，涉及两个播报文件 |

故障测试使用真实管道、真实子进程和 FFmpeg。模型及声卡由确定性替身代替。
文件测试检查输出 WAV 的数据样本数。超出输入时长的固定窗口结果仍按既有语义裁剪。

完整日志保存在本机目录：

`/home/moyamryia/Projects/Project-Mozart-backups/demo-fixes-validation/`

## 子代理审查处置

审查检查了重复校验、测试独有守卫、重复阈值、异常处理、验收条件及文档未解决项。

- 修复 Windows 异常退出误报成功、变长回调分帧和资源关闭顺序。
- 修复播报拒绝与关闭管道的竞争、其他待播任务无法结束，以及提交和取消时的 SQLite 故障。
- 删除重复的内部文件描述符检查、互斥的拒绝守卫和冗余的播放任务清理分支。
- 保留两端已有的排空封装，避免在本轮引入跨进程架构调整。两端执行相同的故障回归。
- 保留 FIONREAD 失败后记录原因并按空水位处理的既有策略。管道的实际打开和读取失败则触发服务退出。
- 保留固定窗口输出的上限裁剪；新增独立的 WAV 样本数断言验证裁剪结果。
- 保留 coverage 的磁盘等待任务，维持重启恢复语义；组件状态明确记录停止原因。
- 预设 HTTP 错误测试使用失败控制器夹具；正常保存和读取由真实 ModeController 测试覆盖。

## 适用范围

配置：main `63479a3` 上的本轮工作区；x86_64；Python 3.10；native Debug；ONNX 与 TensorRT 关闭。
样本数及统计范围见“检查结果”。实时推理算法和模型资产保持原样。
未验证项：Windows 驱动与声卡、Jetson USB、真实模型播报和整链演示。阻塞：Windows 真机未接入，Jetson 已关机。解除条件：连接设备后执行启动、双工录放、拔插恢复和持续运行彩排。
跳过项：5 项检查点测试缺少 PyTorch。解除条件：在安装 PyTorch 的环境运行同一 Python 检查阶段。
PR #10、#11、#12 的真实模型验收仍按各 PR 的适用范围执行。
