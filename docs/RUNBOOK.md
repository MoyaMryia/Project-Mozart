# 运行与调试手册

原生后端、识别、翻译和播报运行在 Jetson。开发机可以编辑源码、构建前端并访问控制台。
本手册中的构建和启动命令均从相应机器的仓库根目录执行。

## 构建与配置

先确认模型、Python 环境、音频设备及推理库已准备好。
原生依赖与模型目录见[部署指南](../frontend/DEPLOYMENT.zh-CN.md)。

```bash
cmake -S . -B build-gpu -DCMAKE_BUILD_TYPE=Release -DUSE_ONNX=ON
cmake --build build-gpu -j4
cmake -S preprocessor -B build/usb-pre -DCMAKE_BUILD_TYPE=Release
cmake --build build/usb-pre -j4
```

根构建产生 `build-gpu/state/mozart_stated` 和根构建树中的预处理程序。
统一实时入口固定使用 `build/usb-pre/mozart-pre`，因此更新预处理源码后也要重建该目录。
播报变速库使用 `build-gpu/preprocessor/libmozart-tempo.so`，由可用的 FFmpeg 开发库决定是否构建。

检查现有 Python 环境：

```bash
.venv/bin/python -c 'import numpy, soundfile, sherpa_onnx'
.venv/bin/python -c 'import pypinyin, jieba'
```

术语纠错的依赖说明见[README](../README.md#python-依赖)。
按部署位置配置 RVC 资产、识别模型、翻译模型与参考音频。
参数名和覆盖方式见[实时入口](../tools/REALTIME_VOICE.md#默认配置)。

## 日常实时启动

启动变声、字幕与翻译：

```bash
tools/run-realtime-voice.sh start
tools/run-realtime-voice.sh status
```

选择译文播报时，先停止已有实例，再带参考音频启动：

```bash
tools/run-realtime-voice.sh stop
OUTPUT=speech REFERENCE=/path/to/reference.wav tools/run-realtime-voice.sh start
```

两种输出均运行字幕与翻译。输出选择与准备条件见[输出开关](../tools/REALTIME_VOICE.md#输出开关)。
需要提前输出音频时，按[提前输出说明](../tools/REALTIME_VOICE.md#提前输出音频)指定独立运行时。

查询和切换设备：

```bash
tools/run-realtime-voice.sh devices
tools/run-realtime-voice.sh pre-restart --capture plughw:CARD=MIC,DEV=0 --playback plughw:CARD=Device,DEV=0
```

设备切换保留字幕发送目标。完整停止使用：

```bash
tools/run-realtime-voice.sh stop
```

页面中的停止播报只停止语音会话，字幕与翻译继续运行。
脚本 `stop` 回收本次管理的预处理、后端、识别、翻译和播报进程。

## 组件与端口

| 组件 | 统一实时入口 | 独立后端或播报启动器默认值 |
| --- | --- | --- |
| HTTP API | TCP 18181 | TCP 18080 |
| RVC 音频 | UDP 18101 | UDP 18000 |
| 识别音频 | UDP 18100 | UDP 18100 |
| 播报服务 | TCP 18081，仅播报模式启动 | TCP 18081，仅 full 模式启动 |
| 翻译服务 | TCP 18200 | TCP 18200 |
| 控制台静态服务 | TCP 8080，单独启动 | TCP 8080，单独启动 |

HTTP 与 RVC 端口由后端配置决定。更改 `API` 时，同步更改配置中的控制端口。
启动器的 `--api-port` 与配置文件保持一致。
预处理 UDP 控制端口与 USB 桥的独立端点见[预处理设计](../DESIGN_PREPROCESSOR.md)。

## 控制台

在开发机构建前端：

```bash
npm --prefix frontend ci
npm --prefix frontend run build
```

可在 Jetson 提供静态页面，或先将 `frontend/dist/` 同步到 Jetson。
针对统一实时入口启动代理：

```bash
python3 tools/serve-console.py --host 0.0.0.0 --port 8080 --api 127.0.0.1:18181
```

浏览器访问 `http://<jetson-ip>:8080/`。独立后端使用 18080 时，相应更改 `--api`。
Vite 开发代理默认指向开发机的 18080。跨机器开发时，在开发机另开 SSH 转发：

```bash
ssh -N -L 18080:127.0.0.1:18181 <jetson-host>
```

随后在开发机运行 `npm --prefix frontend run dev`。
已构建静态服务的参数以实际进程为准；示例 systemd 单元使用默认后端端口。

## 文件转换与独立播报

文件转换的资产、启动、上传和下载步骤见[部署指南](../frontend/DEPLOYMENT.zh-CN.md)。
独立文件后端与统一入口使用各自的端口，启动前检查现有监听和模型占用。

文件回放及独立参考播报使用 Python 监督程序：

```bash
.venv/bin/python tools/run_translated_speech.py \
  --backend-config state/translated-speech.yaml \
  --reference /path/to/reference.wav --input /path/to/input.wav --seconds 120
```

该配置关闭 RVC。省略 `--input` 后使用麦克风。
监督程序的准备、停止与恢复规则见[播报契约](../tools/REFERENCE_SPEECH.md)。

## 本地修改与板端调试

1. 核对两端提交和工作区状态。
2. 在开发机修改源码，并执行对应主机回归。
3. 将改动文件同步到 Jetson，保留板端配置、模型、日志和未跟踪文件。
4. 在 Jetson 重建受影响的构建目录。
5. 核对二进制、资产路径和启动参数。
6. 用固定输入执行板端回归，并保存配置、输出和日志。

`tools/deploy.sh` 用于同机目录同步，目标目录必须已经存在。
它同步部分源码与文档，记录 `BUILD_INFO`，并比较指定源码文件的内容。
`--rebuild` 只构建目标的 `build-gpu`。它不替代跨机器同步、前端构建或 `build/usb-pre` 重建。
同一路径执行该同步也会涉及覆盖操作，应使用独立部署目录。

RVC 音质与导出问题按[Golden 调试流程](../AGENTS.md#rvc-debugging-workflow)定位。

## 状态与校验

统一实时入口运行时查询：

```bash
curl -fsS http://127.0.0.1:18181/api/health
curl -fsS http://127.0.0.1:18181/api/status
curl -fsS http://127.0.0.1:18181/api/stack/status
curl -fsS http://127.0.0.1:18181/api/preprocessor
tools/run-realtime-voice.sh status
```

健康响应只表示 HTTP 存活。模型资产与实际推理后端见 `/api/status`。
字幕输入、服务就绪和降级原因见 `/api/stack/status`。
日志与状态文件位置见[实时日志说明](../tools/REALTIME_VOICE.md#日志与状态)。

`tools/smoke-test.sh` 启动独立文件输入与播报链，检查字幕、译文、变速上限和已有库的新旧状态。
运行前停止占用其默认端口的服务，并通过 `MOZART_SMOKE_INPUT`、`MOZART_REFERENCE` 指定测试资产。
该脚本使用 `--no-playback`，真实音频输出按彩排手册验收。

## USB 音频桥

USB 桥独立于语音启动脚本运行。安装、设备映射与恢复检查见[USB 说明](../usb-audio/README.md)。
在两端分别读取：

```bash
systemctl status mozart-usb-audio --no-pager
journalctl -u mozart-usb-audio -n 20 --no-pager -o cat
```

先核对发送、接收、缺口和错误计数，再按故障位置处理。

## 故障与回滚

| 现象 | 先检查 | 后续动作 |
| --- | --- | --- |
| 启动失败 | `supervisor.log`、端口监听、子进程退出码 | 解决已记录原因，再启动 |
| 字幕断开 | API 代理目标、SSE 连接、字幕 JSONL | 修复连接或上游输入，页面会重新连接 |
| 设备切换失败 | `/api/preprocessor` 的错误与实际设备名 | 确认设备存在并释放占用，再应用设置 |
| 无播报 | 语音会话、运行时和任务错误 | 按状态恢复准备和会话 |
| 变速库过期 | `tools/check-tempo-limit.py` | 重建 `mozart_tempo_filter` 目标 |
| USB 无帧 | 两端服务日志与端点状态 | 按 USB 手册处理对应端点 |

回滚前保留当前改动与日志。停止受影响的链路，再切换到已知提交。
同步匹配的配置，重建两个相关构建目录，再用同一固定输入验证。
提交检查入口为 `tools/ci-local.sh --staged`；各阶段命令由该脚本统一维护。
