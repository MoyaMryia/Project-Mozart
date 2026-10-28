# USB 音频桥

用 Jetson 的 USB-C 口传输全双工音频，取代 3.5 mm 模拟回路。
两端各有一张 `snd-aloop` 虚拟声卡，音频经 FunctionFS bulk 端点传输。

## 背景

模拟回路把播报直接送回同一个 TRRS 插头的麦克风输入，形成 TTS → ASR 自环。
实测串扰：工作站 USB 声卡 17 dB 隔离，Jetson USB 声卡带线 37 dB。
播报泄漏电平 −44.2 dBFS，识别器把英文译文按同音字转写成中文。

回声消除方案实测不够用：扫频信号 ERLE 12.9 dB，真实语音只有 5.2 dB。
线性自适应滤波无法覆盖非线性失真与延迟抖动。

Tegra234 的 USB 控制器不支持 UAC2 等时端点，见 [usb-gadget/DECISION.md](../usb-gadget/DECISION.md)。
因此使用 bulk 端点加自定义帧协议，并在同一链路上预留控制通道。

## 架构

```
应用 ──▶ plughw:CARD=Loopback,DEV=0,SUBDEV=1 ──▶ 主机守护进程
                                                    │  bulk OUT
                                                    ▼
                                              Jetson 守护进程 ──▶ plughw:CARD=Loopback,DEV=0,SUBDEV=0
                                                    │                     │
                                                    │                     ▼
                                                    │              管线麦克风输入
                                                    │
管线扬声器输出 ──▶ plughw:CARD=Loopback,DEV=0,SUBDEV=1 ──▶ Jetson 守护进程
                                                    │  bulk IN
                                                    ▼
                                              主机守护进程 ──▶ plughw:CARD=Loopback,DEV=1,SUBDEV=0
                                                                      │
                                                                      ▼
                                                               应用录音端
```

| 用途 | 设备名 |
| --- | --- |
| 主机：虚拟扬声器（送往 Jetson） | `plughw:CARD=Loopback,DEV=0,SUBDEV=1` |
| 主机：虚拟麦克风（来自 Jetson） | `plughw:CARD=Loopback,DEV=1,SUBDEV=0` |
| Jetson：管线麦克风（来自主机） | `plughw:CARD=Loopback,DEV=1,SUBDEV=0` |
| Jetson：管线扬声器（送往主机） | `plughw:CARD=Loopback,DEV=0,SUBDEV=1` |

`snd-aloop` 按 substream 配对：播放 `dev 0 sub N` 对应采集 `dev 1 sub N`。
一张卡的两个 substream 提供两个独立方向。

## 端点与帧长

设备声明两个 vendor 接口。

| 接口 | 端点（高速） | 使用方 | 帧长 |
| --- | --- | --- | --- |
| 0 | OUT `0x01` / IN `0x82` | 默认路径、Windows 桥 | 128 样本或 20 ms |
| 1 | OUT `0x03` / IN `0x84` | `mozart-pre --usb-endpoint`、主机直连模式 | 128 样本 |

Jetson 的发送帧长由 `MOZART_FAST` 选择。Linux 主机按 128 样本块采集。
直连模式要求 Jetson 侧运行 `mozart-pre --usb-endpoint`，由该进程持有 `ep3`/`ep4`。

## 协议

固定 24 字节帧头，小端序。

| 偏移 | 长度 | 字段 |
| ---: | ---: | --- |
| 0 | 2 | 魔数 `0x4D5A` |
| 2 | 1 | 版本 |
| 3 | 1 | 类型 |
| 4 | 1 | 流 ID |
| 5 | 1 | 标志 |
| 6 | 2 | 保留，必须为 0 |
| 8 | 4 | 序号 |
| 12 | 8 | 发送时间戳，微秒 |
| 20 | 4 | 负载长度 |

类型：`PCM`、`CONTROL`、`TELEMETRY`。
流 ID：`AUDIO`、`CONTROL`、`FAST`。`FAST` 表示 128 样本帧，`AUDIO` 表示 20 ms 帧。
接收方按帧头负载长度处理，两种音频帧长都接收，并拒绝其他长度。
其余类型与流 ID 为预留值，接收方忽略并保持连接。
单帧负载上限为 1 MiB。

20 ms 帧负载为 3840 字节，128 样本帧负载为 512 字节，均为 48 kHz 立体声 16 位 PCM。
控制、字幕、WebUI 复用同一对端点，新增流只增加流 ID。

## 安装

两台机器都先加载 `snd-aloop`：

```bash
sudo cp usb-audio/systemd/mozart-aloop.conf /etc/modules-load.d/
sudo modprobe snd-aloop
```

主机侧要让 PulseAudio 忽略虚拟卡，否则 Pulse 会独占 `hw:Loopback`：

```bash
sudo tee /etc/udev/rules.d/89-pulse-ignore-loopback.rules >/dev/null <<'EOF'
SUBSYSTEM=="sound", KERNEL=="card*", ATTRS{id}=="Loopback*", ENV{PULSE_IGNORE}="1"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=sound
```

两台机器都把程序装到同一路径，单元文件按该路径引用：

```bash
sudo mkdir -p /usr/local/lib/mozart
sudo cp -r usb-audio /usr/local/lib/mozart/
```

主机：

```bash
sudo cp usb-audio/systemd/mozart-usb-audio-host.service /etc/systemd/system/mozart-usb-audio.service
sudo systemctl daemon-reload
sudo systemctl enable --now mozart-usb-audio
```

Jetson：

```bash
sudo install -m 755 usb-audio/mozart-gadget-setup.sh /usr/local/sbin/
sudo cp usb-audio/systemd/mozart-usb-audio-jetson.service /etc/systemd/system/mozart-usb-audio.service
sudo systemctl daemon-reload
sudo systemctl enable --now mozart-usb-audio
```

Jetson 单元会自动停用 `nv-l4t-usb-device-mode.service`。
该服务与自建 gadget 争用同一个 UDC，两者只能启用一个。
需要恢复厂商 USB 串口与网络时，停用 `mozart-usb-audio` 并重新启用该服务。

守护进程写完 FunctionFS 描述符后绑定 UDC，退出时解绑。
`MOZART_UDC` 留空则跳过绑定，用于只做本地声卡调试。

## Windows 主机

Windows 主机不使用 `snd-aloop` 与主机守护进程，改用 [windows-bridge.py](windows-bridge.py)。
该程序直接读写 USB 端点，用系统声卡播放与采集。

准备步骤：

1. 安装依赖：`pip install pyusb sounddevice numpy`。
2. 用 Zadig 安装 WinUSB 驱动：Options → List All Devices，
   选择 `USB Device`（`1d6b:0105`）的接口 0，替换驱动为 WinUSB。
   每台主机执行一次。
3. 启动 Jetson 侧 `mozart-usb-audio` 服务并连接 USB-C 线。

```bash
python windows-bridge.py --play              # 扬声器播放回传音频
python windows-bridge.py --capture           # 本机麦克风送入 Jetson
python windows-bridge.py --play --capture    # 全双工
```

该程序只使用接口 0。接收方向接受两种帧长，发送方向固定 20 ms 帧。
该程序在 Linux 主机验证过录放与采集。

## 离线模式

两个守护进程在 USB 未连接时保持虚拟声卡可用。
主机侧向虚拟麦克风写入静音，并丢弃虚拟扬声器的数据。
Jetson 侧继续消费管线播放，并向管线采集端写入静音。

上层应用照常启动和运行，计数器中的丢弃数增长，状态行带上最近一次错误。
这样可以在没有 USB 连接的情况下单独调试管线。

Jetson 端点读写失败或发送停滞时，守护进程记录错误并以状态码 1 退出。
systemd 单元按 `Restart=always` 重新启动服务，重新写入描述符并绑定 UDC。

采集端按原样发送 PCM。两端只在 USB 接收后、写入播放设备前做自适应重采样。
反馈水位来自播放管道实际积压，字节数换算为音频采样帧；每帧包含两个声道样本。
水位高时减少输出，水位低时增加输出。比例上限为 ±500 ppm，并按 64 块平滑。

## 管线接入

`tools/run_translated_speech.py` 支持两个环境变量覆盖设备默认值：

```bash
export MOZART_MIC_DEVICE=plughw:CARD=Loopback,DEV=1,SUBDEV=0
export MOZART_PLAYBACK_DEVICE=plughw:CARD=Loopback,DEV=0,SUBDEV=1
```

命令行参数 `--mic-device` 与 `--playback-device` 的优先级更高。

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `MOZART_FFS` | FunctionFS 挂载点，默认 `/dev/ffs-mozart` |
| `MOZART_UDC` | 写描述符后绑定的 UDC 名；留空只做本地声卡调试 |
| `MOZART_MIC` / `MOZART_SPEAKER` | Jetson 侧采集与播放设备 |
| `MOZART_FAST` | Jetson 发送档位：`0` 使用 20 ms 帧；默认使用 128 样本帧 |
| `MOZART_DIRECT` | Linux 主机直连模式：`1` 使用物理设备并声明接口 1 |
| `MOZART_DIRECT_MIC` / `MOZART_DIRECT_SPEAKER` | 直连模式的物理设备名 |
| `MOZART_MIC_SINK` / `MOZART_APP_SOURCE` | 主机侧虚拟麦克风与虚拟扬声器设备 |
| `MOZART_TRACE` | 打印帧序号与时间戳打点 |

## 验证

```bash
# 主机播 1 kHz，Jetson 采集
aplay -D plughw:CARD=Loopback,DEV=0,SUBDEV=1 tone-1k.wav
ssh jetson arecord -D plughw:CARD=Loopback,DEV=1,SUBDEV=0 -d 3 -f S16_LE -r48000 -c2 /tmp/rx.wav

# 服务状态与计数
systemctl status mozart-usb-audio
journalctl -u mozart-usb-audio -n 5 -o cat
```

实测数据与测量口径见 [usb-audio-20261020](../reports/usb-audio-20261020/RESULTS.md)。
主机回路的定位与数值见[端到端延迟实测](../reports/e2e-latency-20261025/RESULTS.md)。
计数器每次启动从零开始，日志每 2–5 秒输出一行。

## 适用范围

- 配置：Jetson Orin Nano Super 8GB；接口 0 与 1 的 vendor 描述符；48 kHz、16 位、双声道。
- 样本：端点回环 3000 帧、受压 640 秒（20261020）；e2e 三组，period 64 两组、period 128 一组（20261025）。
