# 演示机运行手册

面向交付后负责值守的人。所有命令在演示机仓库根目录执行。

## 组件与端口

| 组件 | 端口 | 说明 |
| --- | ---: | --- |
| `build-gpu/state/mozart_stated` | 18080 | HTTP API 与状态 |
| `tools/speech_service.py` | 18081 | 语音合成与播放 |
| `tools/stt_service.py` | 18100 | 流式识别 |
| `llama-server` | 18200 | 翻译 |
| `mozart-usb-audio` | — | USB 音频桥，systemd 服务 |

## 部署

```bash
git clone <仓库地址> ~/mozart-demo && cd ~/mozart-demo
cmake -S . -B build-gpu -DCMAKE_BUILD_TYPE=Release -DUSE_ONNX=ON
tools/deploy.sh --target ~/mozart-demo --rebuild
```

`deploy.sh` 同步源码、写入 `BUILD_INFO`、校验哈希、重建并跑冒烟测试。
目标目录已有 `build-gpu` 时只做增量构建。

模型与资产放在仓库外：

```
~/models/sherpa-onnx/clone-test-downloads/sherpa-onnx-pocket-tts-int8-2026-01-26
~/models/sherpa-onnx/clone-test-downloads/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17
~/models/sherpa-onnx/zipformer-zh-14M
~/mozart-archive/qwen35/llama.cpp/build-cuda-bin/llama-server
~/mozart-archive/qwen35/gguf/qwen35-0.8b-text-Q4_K_M.gguf
```

## 启动

```bash
tools/run_translated_speech.py \
  --backend-config state/translated-speech.yaml --mode full \
  --reference ~/Mozart/rvc-golden/clone-tts/qiqi-20261004/reference-qiqi-natural-source.wav \
  --final-model ~/models/sherpa-onnx/clone-test-downloads/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17 \
  --mic-device plughw:CARD=Device,DEV=0 \
  --playback-device plughw:CARD=Device,DEV=0
```

设备名按现场声卡调整。使用 USB 音频桥时，设备从 `MOZART_*` 环境变量读取：

```bash
export MOZART_MIC_DEVICE=plughw:CARD=Loopback,DEV=1,SUBDEV=0
export MOZART_PLAYBACK_DEVICE=plughw:CARD=Loopback,DEV=0,SUBDEV=1
```

演示主机为 Windows 时，按 [USB 音频桥的 Windows 主机步骤](../usb-audio/README.md#windows-主机)
安装 WinUSB 驱动，并运行 `usb-audio/windows-bridge.py`。

停止用 `Ctrl+C`，或在另一个终端 `pkill -f run_translated_speech`。

## 控制台

先把前端构建一次，再用静态服务对外提供页面并转发接口：

```bash
npm --prefix frontend ci
npm --prefix frontend run build
python3 tools/serve-console.py --host 0.0.0.0 --port 8080 --api 127.0.0.1:18080
```

浏览器访问 `http://<演示机>:8080/`。静态文件与 `/api`、`/health`、`/status`、`/models`
同源，前端不需要再配置代理。服务单元见 `systemd/mozart-console.service`。

演示机没有 Node.js 时，在开发机构建后只拷贝产物：

```bash
tar -czf dist.tgz -C frontend dist
scp dist.tgz <演示机>:<仓库>/frontend/
ssh <演示机> 'tar -xzf <仓库>/frontend/dist.tgz -C <仓库>/frontend/'
```

`serve-console.py` 只依赖标准库，有 `frontend/dist` 就能运行。

## 早播音频

`--early-audio` 需要一份打过补丁的 sherpa-onnx 运行时，构建一次即可：

```bash
python3 tools/build_pocket_early_audio.py ~/mozart-early --jobs 4
```

构建输出 `~/mozart-early/python`，启动管线时传入：

```bash
--early-audio --tts-pythonpath ~/mozart-early/python
```

构建脚本校验 sherpa 源码与补丁哈希，不一致会直接失败。不传 `--tts-pythonpath`
时管线使用系统已装的 sherpa-onnx，行为与之前一致。

## 健康检查

```bash
curl -s http://127.0.0.1:18080/api/health
tools/smoke-test.sh 20
journalctl -u mozart-usb-audio -n 20 -o cat
cat BUILD_INFO
```

`smoke-test.sh` 校验三件事：能产出译文、变速上限在 Python 与 C 两侧一致、
变速库不比源码旧。任意一项失败都会以非零退出。

USB 桥的两端计数应同时增长：

```bash
# 主机
journalctl -u mozart-usb-audio -n 1 -o cat
# Jetson
ssh 192.168.10.214 'journalctl -u mozart-usb-audio -n 1 --no-pager -o cat'
```

状态行格式：`发 N 收 M 丢 X 缺口 G 错 E`。`缺口` 与 `错` 停止增长表示链路正常。

## 回滚

```bash
git log --oneline -5
git checkout <上一个提交>
tools/deploy.sh --target ~/mozart-demo --rebuild
```

`BUILD_INFO` 记录当前提交，便于确认回滚结果。

## 常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 守护进程报 `OSError: Invalid argument` | functionfs 描述符状态残留 | `systemctl restart mozart-usb-audio`；安装脚本会重建挂载 |
| 播放启动失败、日志 `Speech worker exited` | 变速库与源码不一致 | `cmake --build build-gpu --target mozart_tempo_filter` |
| `arecord: No such device` | 虚拟声卡未加载 | `sudo modprobe snd-aloop` |
| 端口被占用 | 上一次管线未退出 | `pkill -f run_translated_speech` |
| 帧计数冻结 | 主机重启后端点失效 | 看门狗 3 秒内自动重开；仍不动则 `systemctl restart mozart-usb-audio` |
| 主机名解析失败 | 开机 DNS 未就绪 | 脚本改用局域网地址；网络恢复后手动重试 |
| Windows 主机报未找到 USB 设备 | `1d6b:0105` 未绑定 WinUSB | 用 Zadig 给接口 0 安装 WinUSB，见 [USB 音频桥](../usb-audio/README.md#windows-主机) |

## 版本与检查

提交前运行 `tools/ci-local.sh --staged`。它执行的阶段与 CI 相同：
lint、Python 测试、原生测试、变速、前端。
