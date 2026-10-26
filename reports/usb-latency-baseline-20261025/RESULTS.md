# USB 专用端点与预处理延迟基线

日期：2026-10-25。测试版本：`5b20c6029b9a091e43292284c544e8023397669f`。

## 结论

64 样本块的数字往返中位数为 **1.527 ms**，P99 为 **1.691 ms**，最大值为 **4.745 ms**。
该路径包含 USB 双向传输、高通、WOLA、输出攒包和主机读取。
预处理计算耗时远小于块周期：Jetson 周期测试中，64 样本块的调用耗时中位数为 **27.488 µs**。
这些结果支持继续实施低延迟专用路径。完整产品验收范围见文末。

## 测试路径与计时

```text
本机定时发送完整音频块
  → interface 1 bulk OUT
  → Jetson 原生测试泵
      ├─ echo：原样返回
      └─ dsp：FL → 高通 → WOLA → 输出 FIFO → 固定块打包
  → interface 1 bulk IN
  → 本机读取并验证
```

主机使用 Python/PyUSB；Jetson 使用 C/FunctionFS。
主机发送线程与接收线程并行运行。Jetson 根据收到的数据推进处理，不使用独立音频定时器或 SRC。
Jetson 原有接口 0 守护进程保持运行。本机原有 USB 桥在测量期间停止，结束后恢复。
未修改内核、驱动、UDC 绑定或生产处理代码。

每包计时从主机调用 USB 写入前开始，到 USB 读取返回后结束，使用主机同一单调时钟。
同一次读取包含多个协议包时，各包使用同一个读取完成时刻；该指标表示应用可取得完整包的时刻。
输入块在计时开始前已完整可用。两种块长分别按 48 kHz 的节拍发送。
CSV 另外记录发送时刻相对计划时刻的迟到量。

## 数字往返结果

每种配置重复三次，每次统计 3000 包，合并后每行统计 9000 包。
每轮另外保留前 100 包启动记录和后 16 包尾段记录。
分位数使用 nearest-rank。全部最大值保留。

| 路径 | 块长 | 中位数 | P95 | P99 | 最大值 | 超过 10 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| USB 原样往返 | 64 | 0.176 ms | 0.233 ms | 0.322 ms | 0.612 ms | 0/9000 |
| USB＋预处理 | 64 | 1.527 ms | 1.623 ms | 1.691 ms | 4.745 ms | 0/9000 |
| USB 原样往返 | 128 | 0.194 ms | 0.234 ms | 0.315 ms | 0.461 ms | 0/9000 |
| USB＋预处理 | 128 | 2.910 ms | 2.999 ms | 3.050 ms | 6.182 ms | 0/9000 |

六轮 DSP 测试的首次回包耗时为 **10.605–11.133 ms**，对应 WOLA 初始化积累。
稳态结果存在长尾。64 样本配置的最大值与中位数相差 **3.217 ms**。

本表已经计入 DSP 等待和输出攒包。旧报告的 4 ms 为另一种输入分块与发射轨迹口径。
不要把该 4 ms 再加到本表的 DSP 行。

## 数据完整性

12 轮共发送并收到 **37392/37392 包**。
序号、时间戳、长度和样本计数全部匹配。
Echo 路径逐字节比较负载。DSP 路径逐样本比较本机当前 C 实现的参考输出。
该比较验证跨平台计算及传输一致性；音频质量使用独立语音测试验收。

DSP 比较允许 **1 个 S16 量化单位**的差异，原因是主机参考转换使用 Python 浮点乘法，板端转换使用 C float 乘法。
实测最大差为 1 个量化单位。修改两个声道的同一参考样本达 100 个量化单位后，检查明确失败。

测试泵使用 FIFO 保存 WOLA 初始化突发输出及不足一包的样本。
结束时补入一个零块，以送出全部原始输入对应的输出。
主机验证输入和输出采样帧数相等，并核对补零数量及剩余补零样本数。
64 样本块每轮核对 **199424/199424 个采样帧**；128 样本块核对 **398848/398848 个采样帧**。

## Jetson 计算耗时

计时函数为 `mozart_dsp_enhance()`，包含高通、WOLA 和配置中的整形级。
输入生成与输出检查在计时区间之外。输入为确定性宽带噪声叠加正弦。
Wall 使用 `CLOCK_MONOTONIC_RAW`；线程 CPU 时间使用 `CLOCK_THREAD_CPUTIME_ID`。
Wall 区间包含两次线程 CPU 时钟读取开销。

| 配置 | 调用方式 | 中位数 | P99 | 最大值 | 样本数 |
| --- | --- | ---: | ---: | ---: | ---: |
| 64，降噪开，整形关 | 紧循环 | 11.360 µs | 11.680 µs | 25.472 µs | 100000 块 |
| 128，降噪开，整形关 | 紧循环 | 21.568 µs | 22.144 µs | 40.000 µs | 100000 块 |
| 64，降噪和全部整形开 | 紧循环 | 12.192 µs | 12.704 µs | 41.024 µs | 100000 块 |
| 128，降噪和全部整形开 | 紧循环 | 23.040 µs | 29.504 µs | 138.528 µs | 100000 块 |
| 64，降噪开，整形关 | 按周期调用 | 27.488 µs | 28.928 µs | 29.664 µs | 3750 块 |
| 128，降噪开，整形关 | 按周期调用 | 52.288 µs | 82.912 µs | 96.864 µs | 1875 块 |

每种紧循环配置先预热 5000 块。两种周期配置各预热 5000 块，再测量 5 s。
周期测试采用绝对时间睡眠。64 样本块周期为 1.333 ms，128 样本块周期为 2.667 ms。
周期测试完成时刻超过下一周期的次数分别为 **0/3750** 和 **0/1875**。

输出容量为 2048，覆盖启动排干突发。64 样本输入首次在第九块输出 512 个样本。
128 样本输入首次在第五块输出 576 个样本。原始数据保留完整启动输出。

## 工件与复现

本轮本地工件目录为 `rvc-golden/usb-latency-20261025/`：

- `endpoint_probe.c`：板端测试泵。
- `host_probe.py`：主机定时发送、接收、完整性检查和统计。
- `usb/*.csv`：全部逐包时间；`usb/results.json`：各轮摘要。
- `endpoint.log`：板端输入、输出和尾段计数。
- `mozart-pre-cpu-20261025/bench.c`：计算基准。
- `mozart-pre-cpu-20261025/{tight,paced,first-output}.csv`：逐块和启动数据。
- `mozart-pre-cpu-20261025/results.json`：计算摘要。

板端编译命令：

```sh
cd ~/Mozart
cc -std=c11 -O3 -fno-fast-math -ffp-contract=off -Wall -Wextra -Werror \
  -I preprocessor/include -I IO/include \
  /tmp/mozart-usb-latency-20261025/endpoint_probe.c \
  preprocessor/src/dsp.c preprocessor/src/noise_suppressor.c \
  preprocessor/src/stages.c -lm \
  -o /tmp/mozart-usb-latency-20261025/endpoint_probe
```

主机参考编译命令：

```sh
cc -std=c11 -O3 -fno-fast-math -ffp-contract=off -shared -fPIC \
  -I preprocessor/include -I IO/include \
  preprocessor/src/dsp.c preprocessor/src/noise_suppressor.c \
  preprocessor/src/stages.c -lm \
  -o rvc-golden/usb-latency-20261025/native-reference.so
```

主机测试命令：

```sh
sudo python3 rvc-golden/usb-latency-20261025/host_probe.py \
  --output rvc-golden/usb-latency-20261025/usb --count 3000 --repeats 3
```

运行前停止本机 USB 桥，并启动板端专用测试泵。为停止流程设置服务恢复处理。
测试后停止测试泵，并确认两端 `mozart-usb-audio` 均为 `active`。
本轮已完成上述恢复确认。

## 适用范围

- 配置：本机 x86_64、Linux 6.14.11；Jetson Orin Nano Engineering Reference Developer Kit Super、Linux 6.8.12-1021-tegra、CPU governor 为 `schedutil`。
- USB：480 Mbps；interface 1，实际枚举地址为 OUT `0x02`、IN `0x82`；包头 24 B；48 kHz、双声道 S16、64/128 采样帧。
- 软件：专用测试泵调用当前 DSP 源码，替代生产守护进程与 `main.c` 搬运路径。RNNoise、SRC 和 20 ms AI 支路均未接入。
- 样本数：数字往返 12 轮、37392 包，其中稳态统计 36000 包；CPU 紧循环每配置 100000 块；周期测试各 5 s。
- 数字计时未纳入本机采集攒块、ALSA 播放排队和 ADC/DAC，因此完整物理端到端尚未验证。阻塞：尚未建立用于测量的本机模拟回录通路。解除条件：接入本机原生音频收发并建立回录通路，使用同一脉冲测量全程。
- AI 并行负载与长时稳定性未验证。阻塞：本轮限定为短时分层基线。解除条件：固定 AI 负载后执行长时回路测试，并统计全部超限事件。
- 工件目录由现有 `.gitignore` 排除，原始数据保留在本机上述路径。语音降噪质量沿用独立测试计划。
