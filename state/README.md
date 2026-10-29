# 状态管理模块

`state` 提供后端入口 `mozart_stated`。守护进程组合 HTTP API、RVC 管线、实时工作线程和文件任务队列。
它默认进入 `IDLE`。字幕与参考音色播报由独立 Python 监督程序管理。

## 构建与启动

从仓库根目录执行：

```bash
cmake -S . -B build-gpu -DCMAKE_BUILD_TYPE=Release -DUSE_ONNX=ON
cmake --build build-gpu -j4
./build-gpu/state/mozart_stated rvc-backend/config.yaml
```

根构建需要 nlohmann/json、spdlog、yaml-cpp、ALSA 开发文件，以及所选推理运行时。
独立构建 `rvc-backend/` 只产生运行时库和测试，不产生守护进程。
部署步骤见 [前端与后端部署](../frontend/DEPLOYMENT.zh-CN.md)。接口见 [API.md](API.md)。

## 组件职责

```text
HTTP API -> StateManagerDaemon -> ModeController
                                  -> RealtimeRvcWorker -> UDP IO -> AudioWorker
                                  -> FileRvcWorker -> FFmpeg -> RVC
```

- `StateManagerDaemon` 读取配置，创建组件，并管理退出顺序。
- `ModeController` 管理模式、模型选择、文件队列和待切换请求，不处理 PCM 样本。
- `RealtimeRvcWorker` 管理 UDP 流和 `AudioWorker` 的启停。
- `FileRvcWorker` 管理解码、可选 WOLA 谱降噪、推理、输出文件和取消检查。

[architecture.html](architecture.html) 保留早期架构示意。当前实现和接口以本文及 API 文档为准。

## 模式与资源

| 模式 | 当前行为 |
| --- | --- |
| `idle` | 停止实时流和文件消费；已加载 RVC 引擎仍可常驻 |
| `rt_rvc` | 打开 UDP 契约流，启动实时处理；文件队列等待 |
| `file_rvc` | 关闭实时流，串行处理文件队列 |
| `rt_zero_shot` / `file_zero_shot` | 未实现，HTTP 返回 501 |

实时与文件 RVC 不同时推理。切出实时模式时先停止工作线程并关闭 UDP 流。
文件任务忙时，切出文件模式的请求写入一个待切换槽；当前任务结束后执行。
同模式重复请求和模型切换的忙碌处理见 API 文档。

`IDLE` 保留已加载引擎。模型管理器在切换后回收多余的闲置缓存，规则见[系统设计](../DESIGN.md#模型与运行时)。

RVC 的 UDP 工作线程不直接占用麦克风。物理采集和播放由外部 `mozart-pre` 使用 ALSA 完成。
切换守护进程模式不会停止外部采集进程。`IO/PipeWireStream` 仍是占位实现。

## 音频契约

唯一结构定义为 [`IO/include/mozart/frame_meta.h`](../IO/include/mozart/frame_meta.h)。
每帧包含 16 字节元数据和单声道 float32 PCM，帧长为 20 ms。

| 帧 | 采样率 | 样本数 | 内存字节数 |
| --- | --- | --- | --- |
| raw | 48 kHz | 960 | 3856 |
| input | 16 kHz | 320 | 1296 |
| output | 48 kHz | 960 | 3856 |

UDP 在元数据前增加 4 字节 MZRT 魔数。输入包为 1300 字节；输出包为 3860 字节。
输出包在常见 1500 字节 MTU 下需要 IP 分片，网络测试必须记录丢包。

## 文件队列与存储

文件队列默认允许 50 个未完成任务。终态历史不占该容量；内存中的历史任务尚无单独数量上限。
队列消费并发度为 1。暂停队列不会中断当前任务。

取消活动任务后，状态先变为 `cancelling`。工作线程终止并回收 FFmpeg 子进程。
RVC 在阶段边界和分块循环中响应取消，单次模型调用返回后进入后续检查点。
队列、文件大小、解码时长和超时规则见[文件接口](API.md#file-routes)。

临时文件位于 `storage.temp_dir`。任务结束后，缓存超过高水位时向其 80% 清理。
排队、处理和取消中的任务文件受到保护；该目标不是硬磁盘配额。
任务队列保存在内存中，重启后不恢复排队任务。

## 验证

主机回归覆盖队列容量、取消状态、模式互斥和缓存保护。
测试命令见 [API.md](API.md#regression-tests)。这些测试不验证模型音质、GPU 性能或物理设备。

HTTP 已设置连接数与请求截止时间，见[HTTP 限制](API.md#current-http-limits)。
剩余工作与解除条件统一记录在[任务清单](../TODO.md)。
