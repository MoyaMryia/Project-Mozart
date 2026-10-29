# Mozart 部署手册

本手册说明如何在 Jetson Orin Nano Super 8GB 与 Linux 主机上部署演示链路。
命令均在仓库根目录执行。

## 1. 硬件与系统

| 项目 | 要求 |
| --- | --- |
| 计算板 | Jetson Orin Nano Super 8GB，JetPack 6，内核 6.8 |
| 主机 | Linux 或 Windows 10 以上；Linux 主机使用 `snd-aloop` |
| 连接 | USB-C 数据线，支持数据传输 |
| 声卡 | Jetson 侧 USB 声卡或 `snd-aloop` 虚拟声卡 |
| 供电 | Jetson 原装电源，演示时保持供电稳定 |

## 2. 依赖

```bash
sudo apt install -y cmake ninja-build g++ libasound2-dev libyaml-cpp-dev \
  nlohmann-json3-dev libspdlog-dev ffmpeg sox
python3 -m venv .venv
.venv/bin/pip install numpy soundfile sherpa-onnx pyusb
npm --prefix frontend ci
```

ONNX Runtime 与 TensorRT 按 `README.md` 配置。
缺少 ONNX Runtime 头文件或库时，配置阶段会失败。

## 3. 部署模型资产

从提交包的 `模型资产/` 安装：

```bash
模型资产/安装模型.sh /path/to/Project-Mozart
```

也可以手工放置：

| 资产 | 路径 |
| --- | --- |
| HuBERT、RMVPE | `rvc-backend/assets/hubert/`、`rvc-backend/assets/rmvpe/` |
| 变声音色 | `rvc-backend/models/<model_id>/` |
| 识别、TTS、翻译模型 | `$HOME/models/`、`$HOME/mozart-archive/qwen35/` |

## 4. 构建

```bash
cmake -S . -B build-gpu -DCMAKE_BUILD_TYPE=Release -DUSE_ONNX=ON
cmake --build build-gpu -j4
npm --prefix frontend run build
```

## 5. 配置音频桥

两端都加载 `snd-aloop`：

```bash
sudo cp usb-audio/systemd/mozart-aloop.conf /etc/modules-load.d/
sudo modprobe snd-aloop
```

主机侧让 PulseAudio 忽略虚拟卡：

```bash
sudo tee /etc/udev/rules.d/89-pulse-ignore-loopback.rules >/dev/null <<'EOF'
SUBSYSTEM=="sound", KERNEL=="card*", ATTRS{id}=="Loopback*", ENV=PULSE_IGNORE="1"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=sound
```

安装桥服务。主机：

```bash
sudo cp usb-audio/systemd/mozart-usb-audio-host.service /etc/systemd/system/mozart-usb-audio.service
sudo systemctl daemon-reload && sudo systemctl enable --now mozart-usb-audio
```

Jetson：

```bash
sudo install -m 755 usb-audio/mozart-gadget-setup.sh /usr/local/sbin/
sudo cp usb-audio/systemd/mozart-usb-audio-jetson.service /etc/systemd/system/mozart-usb-audio.service
sudo systemctl daemon-reload && sudo systemctl enable --now mozart-usb-audio
```

先启动主机侧，再启动 Jetson 侧。

## 6. 一键启动

```bash
tools/bring-up.sh start
```

脚本依次完成：加载 `snd-aloop`、启动桥、等待桥状态就绪、选择声卡、启动实时栈。
声卡优先使用 `CARD=MIC` 与 `CARD=Device`；缺失时退回 USB 桥的 Loopback 对。
查看状态与停止：

```bash
tools/bring-up.sh status
tools/bring-up.sh stop
```

输出中的关键项：

| 项 | 期望 |
| --- | --- |
| 桥 | `active` |
| 后端 | `mode=rt_rvc`，`errors=0`，`late=0` |
| 字幕 | `state=running`，`phase=capturing` |
| 预处理 | `state=ready`，`send_errors=0` |

## 7. 验证

```bash
curl -s http://127.0.0.1:18181/api/status | head -c 200
arecord -D plughw:CARD=Loopback,DEV=1,SUBDEV=0 -f S16_LE -r 48000 -c 2 -d 3 /tmp/rx.wav
```

控制台地址为 `http://127.0.0.1:8080/`，由 `tools/serve-console.py` 提供。
字幕事件位于 `/api/subtitles`。

## 8. 故障排查

| 现象 | 处理 |
| --- | --- |
| 启动时报“USB 桥不可用” | 检查对端守护进程与 USB 连接；`systemctl status mozart-usb-audio` |
| 只列出 Loopback 设备 | 物理声卡未接；桥设备仍可用 |
| 术语表保存失败（HTTP 409） | 启动时未传术语表路径；默认写 `state/glossary.json`，检查该目录可写 |
| 后端加载失败 | 检查 `rvc-backend/config.yaml` 的资产路径与模型元数据 |
| 首帧无声 | 检查 `realtime_playout_delay_ms` 与播放设备 |

## 9. 停止

```bash
tools/bring-up.sh stop
sudo systemctl stop mozart-usb-audio
```

## 适用范围

- 配置：Jetson Orin Nano Super 8GB，JetPack 6；Linux 主机；`snd-aloop`。
- 验证：脚本 `tools/bring-up.sh` 在 Jetson 上执行 `start`、`status`、`stop` 均成功，后端与字幕进入运行状态。
- 未验证：Windows 主机的完整部署流程。阻塞：本机只验证 Linux 主机与 Jetson。解除条件：在 Windows 主机执行 `usb-audio/windows-bridge.py` 全流程并记录结果。
