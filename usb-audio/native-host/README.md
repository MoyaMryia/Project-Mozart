# 原生 USB host

本程序通过 USB bulk interface 1 收发音频。实时模式使用独立采集、接收和播放线程。
文件模式使用同一 USB 链路，并且不打开 ALSA。
正式启动、Jetson `~/Mozart` 部署、权限与旧服务回退步骤见[上级说明](../README.md#正式-native-路径)。

## 构建与测试

```sh
usb-audio/run-native-host.sh build
ctest --test-dir build/native-host --output-on-failure
```

父工程可以使用 `add_subdirectory(usb-audio/native-host)`。
协议单一源为 [`../protocol.json`](../protocol.json)。
`tools/gen_usb_protocol.py` 生成 `IO/include/mozart/usb_protocol_generated.h` 和 Python 常量。
libusb 使用 `third_party/libusb.h`，CMake 通过 `find_library` 查找运行库。

## 命令行

在仓库根目录使用启动脚本：

```sh
MOZART_CAPTURE=hw:CARD=sofhdadsp,DEV=0 \
MOZART_PLAYBACK=hw:CARD=sofhdadsp,DEV=0 \
usb-audio/run-native-host.sh run \
  --periods 6 --prefill 4 --target 3 --seconds 30

usb-audio/run-native-host.sh run \
  --input input.wav --output build/run/processed.wav --pace --seconds 0
```

启动脚本默认采集和播放设备均为 `hw:CARD=sofhdadsp,DEV=0`。
通过 `MOZART_CAPTURE`、`MOZART_PLAYBACK` 配置设备；脚本拒绝 `--capture`、`--playback`。
`MOZART_PULSE_SINK` 指定暂时挂起的 sink，默认使用 PulseAudio 默认 sink。
文件模式跳过 PulseAudio 操作。
输出和 trace 路径必须位于本仓库 `build/run/`，状态路径由脚本管理。

二进制为 `build/native-host/mozart-usb-host`。
直接调用二进制时，`--capture` 和 `--playback` 的默认值均为 `default`。
正式运行使用脚本提供的 FIFO 40 配置与恢复流程。
`--seconds N` 按源采样帧计数。`0` 表示没有时长截止条件。
文件模式在 EOF 处结束；有限时长使用 `min(N × 48000, 文件源长度)` 个源采样帧。
程序把最后一个不足块长的块补零，并在状态中记录 `padding_frames`。
输出 WAV 保存全部 USB 回复，包括预热处理结果；输出文件裁掉输入尾部补零。
实时模式在预热完成后播放回复。`played` 统计预热后的已消费块数。

`--input` 与 `--output` 必须同时提供。输入格式为 48 kHz、双声道、S16 PCM RIFF WAV。音频和记录路径使用普通文件。
`--pace` 按采样时钟发送文件块。实时模式使用采集设备节拍。
显式设备参数与文件模式互斥。

`--legacy` 跳过握手，固定使用 stream 2、128 帧块，并记录 `legacy_requested`。
默认握手发送 HELLO 提议。完整 HELLO_ACK 选择 64 后，START_ACK 才提交 stream 3。
仅 HELLO 的 500 ms 等待到期允许回退，原因是 `hello_timeout_legacy128`。
合法 HELLO_ACK 选择 128 时，原因是 `hello_selected_legacy128`。
协议格式错误、USB 错误和 START_ACK 错误均终止运行。

## 播放与停止

启动播放线程先写入 `prefill` 个真实静音块，并确认 ALSA 已运行。
采集线程随后启动。播放线程在预热期间继续维持启动静音。
前 100 个真实输入块完成 USB 处理，但实时播放跳过对应回复。
播放队列最多保存 8 个块。队列满时，程序立即失败。
进入实际音频播放后，程序保留全部回复，并在 ALSA 欠载时失败。

SIGINT、SIGTERM、有限时长和文件 EOF 均触发正常停止。
TX 停止采集并发送 STOP。RX 等待全部音频回复及 STOP_ACK。
程序验证两个远端块计数、每个序号和本地 TX/RX 覆盖率。
64 帧模式同时验证回传的 host 时间戳。128 帧模式仅用本地主机发送时间计算 RTT。
播放线程清空队列后启动一次非阻塞 ALSA drain，并等待 drain 完成。
USB 传输、回复等待、ALSA 传输、target 等待和 drain 都有截止时间。
线程异常触发共享失败状态。ThreadJoiner 在作用域退出时取消并回收线程。

## 状态与可选记录

启动脚本的状态路径是 `build/run/usb-native-host/status.json`。
直接调用二进制时，默认状态路径是 `build/run/usb-native.json`。
主线程每 100 ms 更新一次状态，并用同目录临时文件与 rename 原子替换。
最终状态为 `stopped` 或 `failed`。首次失败原因在后续收尾期间保留。
运行错误退出码为 `1`；成功退出码为 `0`。
参数错误在启动前返回 `2`，并通过标准错误输出原因。参数错误保持现有输入和状态文件不变。

状态包括协商结果、块长、流、TX/RX/预热/消费计数、源长度、尾部补零和队列水位。
`xrun` 分别记录采集 overrun、播放 underrun 和两个方向的 suspend。
`alsa_queue_estimate` 保存最近的 ALSA delay 读数，并明确标记为估算值。
`rtt_us` 覆盖全部收到的块，包括预热块；起点为主机提交 USB 发送前，终点为主机解码回复后。
直方图使用 4096 个固定桶，桶宽为 32 us。分位数使用桶上界与实测最大值中的较小值。

仅显式指定 `--trace-dir PATH` 时，程序保存 `capture.wav`、`response.wav` 和 `packets.csv`。
专用记录线程执行磁盘写入。TX/RX 只提交到有界记录队列，记录队列满时失败。
trace WAV 包含预热结果，并裁掉最后一个块的源外补零。
CSV 的 `wire_timestamp_us` 在 legacy 模式属于远端时钟；`rtt_us` 始终来自主机时间关联。

## 记录与验收

在 Jetson 预处理已启动时，于主机仓库执行：

```sh
usb-audio/run-native-host.sh run --seconds 30 \
  --trace-dir build/run/native-trace
```

保留该次运行的 `status.json`，再启动下一次会话。
启动脚本会在下一次启动时清除旧状态文件。

[`tools/verify_native_usb.py`](../../tools/verify_native_usb.py) 读取三项来源：

- host trace：`capture.wav`、`response.wav`、`packets.csv`。
- 同次运行结束后的 host `status.json`。
- 当前 C DSP 参考库 `libmozart_pre_reference.so`。

参考库由 `preprocessor/CMakeLists.txt` 的测试构建生成。
在主机仓库构建与 Jetson 相同版本、相同 DSP 配置的参考库：

```sh
cmake -S preprocessor -B build/usb-pre \
  -DCMAKE_BUILD_TYPE=Release -DMOZART_USE_RNNOISE=OFF -DMOZART_BUILD_TESTS=ON
cmake --build build/usb-pre --target mozart_pre_reference --parallel 2
python3 tools/verify_native_usb.py \
  --trace-dir build/run/native-trace \
  --status build/run/usb-native-host/status.json \
  --reference-lib build/usb-pre/libmozart_pre_reference.so \
  --output build/run/native-verify.json
```

该命令使用默认高通＋WOLA 配置；保持 gate、AGC、limiter 关闭。
参考处理读取完整采集源，按协商块长输入当前 C DSP，并补零排空后裁到源长度。
验收逐样本比较双声道回复，最大允许误差为 1 LSB（S16 量化单位）。

trace 保留全部预热音频和逐包记录。
实时播放跳过前 100 包；稳态 RTT 统计排除这些包。
状态的 `rtt_us` 仍覆盖全部回复，验收工具分别核对全部与稳态统计。
STOP 验收覆盖全部 TX/RX 序号、时间戳、源长度、补零、音频长度和最终计数。
正常结束必须为 `stopped`，且 badcount 与 xrun 均为零。
音频长度或计数不符、缺包、超预算和逐样本误差超限都会使工具返回失败。

`alsa_e2e_queue_estimate_us` 按每个稳态播放包计算：

```text
estimate_us = delta_us + floor((capture_frames + playback_frames) × 1000000 / RATE)
delta_us = 播放写入后的主机时间 − 该包 USB 发送前的主机时间
```

`capture_frames` 与 `playback_frames` 来自该包关联的 ALSA delay 读数。
采集块长项与播放队列头的块长项抵消，`delta_us` 已含软件等待，不另加 WOLA 常数。
状态保存分位数、超预算包数和最大值证据；工具独立复算最大值并核对稳态覆盖数。
数字段预算为 10 ms，单一源是 `protocol.json` 的 `DIGITAL_LATENCY_BUDGET_US`。
工具默认从该字段读取预算；`--steady-budget-ms` 仅显式覆盖稳态 RTT 验收预算。

## 适用范围

- 构建配置：C++17、ALSA 非阻塞接口、vendored libusb 头与系统 libusb 运行库。
- 单元测试覆盖协议 schema、golden 字节、拆合包、非法帧、序号及时间戳关联、超时、队列满、状态、参数、FIFO 拒绝和 WAV。
- RIFF WAV 使用 32 位数据长度；达到边界时程序报错停止。
- 单次会话使用 32 位 USB 块计数；计数范围耗尽时程序报错停止。
- 实测配置、样本数与指标范围见[正式链路报告](../../reports/usb-native-20261026/RESULTS.md#适用范围)。
