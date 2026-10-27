# 七七索引检索接入

本文件记录 C++ 后端打开索引检索的配置与实测。
模型目录为 `rvc-backend/models/qiqi-zh/`，逐文件软链指向真实资产。

## 资产

| 文件 | 目标 | SHA-256 |
|---|---|---|
| `qiqi-zh.onnx` | `rvc-golden/qiqi-zh-run/qiqi-zh-dynamic.onnx` | — |
| `qiqi-zh-front.engine` | `qiqi-zh-realtime/qiqi-zh-realtime-front.engine` | — |
| `qiqi-zh-decoder.engine` | `qiqi-zh-realtime/qiqi-zh-realtime-decoder.engine` | — |
| `qiqi-zh.index` | `.../七七/added_IVF977_Flat_nprobe_1_default_exp_name_v2.index` | `a53faa98e286aa655394e5a74050ee7f2b74466ea066a0feb0e1025a8743d6af` |
| `qiqi-zh.pth` | `.../七七/七七.pth` | `d72bd33911b8a9d2ccac81dc005998a2c043e97587374cecf3ca38edef05d64d` |

索引包含 38109 条 768 维向量，nlist 977。

## 配置

`rvc-golden/qiqi-zh-run/backend-index.yaml`：

- 端口 18101 / 18181。
- `pitch_shift: 9`、`index_rate: 0.5`、`index_nprobe: 16`。
- `filter_radius: 3`、`rms_mix_rate: 1.0`、`protect: 0.33`。

启动：

```bash
./build-gpu/state/mozart_stated rvc-golden/qiqi-zh-run/backend-index.yaml
```

切换模型并检查索引：

```bash
curl -X POST http://127.0.0.1:18181/api/mode/switch \
  -H 'Content-Type: application/json' \
  -d '{"mode":"rt_rvc","model_id":"qiqi-zh"}'
```

状态接口的 `model.has_index` 应为 `true`。

## 实测

输入为 `test-audio.mp4` 前 30 s 经 16 kHz 转换后的同一段录音。
UDP 流式测试收发 1701/1701 帧，141 块，错误、晚到、丢包均为 0。
输出 1439040 个样本、48 kHz。

| 项 | 结果 |
|---|---|
| 索引检索相对关闭时 | 2.91 dB |
| 索引检索相对关闭时 log-mel 相关 | 0.9835 |
| C++ 与 PyTorch 参考 log-mel 相关 | 0.9799 |
| C++ 输出中位 F0 | 181.8 Hz |
| PyTorch 参考中位 F0 | 183.9 Hz |

PyTorch 参考在同一 `index_rate` 下与关闭索引相差 2.90 dB。
C++ 的 2.91 dB 与该值一致，确认检索生效。

试听文件在 `~/Projects/RVC-official/output/liuying-testaudio-polish/`：
`95-cpp-backend-qiqi-idx05.wav`、`96-cpp-backend-qiqi-idx0.wav`。

## 实现变更

- `IndexSearch` 由单邻居改为 k=8 加权平均，权重为 `(1/distance)^2` 归一化，与 Golden 路径一致。
- 候选不足 8 个时整帧跳过，计数在 `skipped_frames()`。
- 新增 `index_nprobe` 参数：范围 1–1024，默认 1，可由 yaml 或 `PUT /api/parameters` 设置。
- `RVCModel::load` 在索引文件存在时加载。内存占用：七七 117 MB，流萤 344 MB。
- `rvc-backend/tests/gen_index_fixture.py` 与 `test_index_search.cpp` 按 k=8 语义重新生成并对照。

## 启动脚本

`tools/run-realtime-voice.sh` 一次拉起后端与预处理。

```bash
tools/run-realtime-voice.sh start     # 启动
tools/run-realtime-voice.sh status    # 状态
tools/run-realtime-voice.sh restart   # 重启
tools/run-realtime-voice.sh stop      # 停止
```

默认值：

| 变量 | 默认 |
|---|---|
| `CONFIG` | `rvc-golden/qiqi-zh-run/backend-index.yaml` |
| `CAPTURE` | `plughw:CARD=MIC,DEV=0` |
| `PLAYBACK` | `plughw:CARD=Device,DEV=0` |
| `MODEL` | `qiqi-zh` |
| `PITCH` | `11` |
| `INDEX_RATE` | `0.5` |
| `INDEX_NPROBE` | `16` |
| `API` | `http://127.0.0.1:18181` |
| `AUDIO_PORT` | `18101` |
| `RUN_DIR` | `/tmp/mozart-realtime` |

启动顺序：后端 → 等 HTTP 就绪 → 切 `idle` → 切 `rt_rvc` → 设参数 → 检查索引 → 预处理 → 等 `state=ready`。

`start` 先停旧进程。端口 18181 或 18101 被其他进程占用时停止并提示。
日志和 PID 在 `RUN_DIR` 下：`backend.log`、`pre.log`、`backend.pid`、`pre.pid`、`pre-status.json`。

## 单模型内存占用

`rvc-backend/models/` 只保留 `qiqi-zh` 时启动，加载完成后的实测：

| 项 | 数值 |
|---|---|
| 进程常驻（VmRSS） | 1939 MB |
| 进程峰值（VmHWM） | 2211 MB |
| 一次 30 秒推理后常驻 | 1901 MB |
| 系统 used | 2408 MB |
| 系统 available | 5137 MB |
| tegrastats RAM | 2300 / 7546 MB |

进程峰值出现在加载阶段，推理期间不再增长。
索引占 117 MB（38109 × 768 × 4 字节），已计入上表。
停止进程后系统 used 回落到 1555 MB。

## 适用范围

- 配置：Jetson Orin Nano Super 8GB；CUDA 13.2；TensorRT 实时前端；ONNX Runtime CUDA 文件生成器。
- 样本：一份 30 秒录音，两个 `index_rate` 取值各运行一次。
- C++ 与 PyTorch 的分块长度不同（141 块对 126 块），波形不逐样本对齐。
- 用户听感待确认。阻塞：尚无本轮试听反馈；解除条件：试听 95 / 96 两个文件并确认索引效果。
- 流萤索引接入未测。阻塞：本轮只接七七；解除条件：确认音色后按同一流程部署流萤。
