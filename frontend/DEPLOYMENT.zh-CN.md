# FILE_RVC 部署与启用说明

本文档用于启动当前 Project Mozart 的 FILE_RVC 完整链路：

```text
浏览器 -> Vite 前端 -> /api 代理 -> mozart_stated -> FILE_RVC Worker
       -> FFmpeg -> 预处理 -> HuBERT / RMVPE / RVC Generator ONNX
```

推荐的后端全局入口是 `mozart_stated`。正常部署时不要使用旧的
`rvc_backend` 可执行文件；它只用于兼容旧脚本。

## 前置条件

在仓库根目录下，以下文件应存在：

```text
build/state/mozart_stated
rvc-backend/config.yaml
rvc-backend/assets/hubert/hubert_base.onnx
rvc-backend/assets/rmvpe/rmvpe.onnx
rvc-backend/models/de_narrator/de_narrator.onnx
rvc-backend/models/de_narrator/config.json
```

检查 ONNX Runtime：

```bash
ldconfig -p | grep onnxruntime
```

检查 FFmpeg：

```bash
ffmpeg -version
```

前端命令需要先通过 nvm 加载 Node.js：

```bash
source ~/.nvm/nvm.sh
node --version
npm --version
```

## 构建

在仓库根目录构建 daemon、RVC runtime、IO 和预处理组件：

```bash
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build -j4
```

构建前端：

```bash
cd frontend
source ~/.nvm/nvm.sh
npm install
npm run build
```

生产环境前端产物位于：

```text
frontend/dist/
```

## 真实 ONNX 配置

daemon 读取的配置文件是：

```text
rvc-backend/config.yaml
```

要启用真实 FILE_RVC 推理，三个组件的 mock 必须都为 `false`：

```yaml
rvc:
  models_dir: "./models"
  hubert_path: "./assets/hubert/hubert_base.onnx"
  rmvpe_path: "./assets/rmvpe/rmvpe.onnx"
  mock:
    generator: false
    hubert: false
    rmvpe: false
```

这些路径相对于 `rvc-backend/config.yaml` 所在目录解析，不受 shell
当前工作目录影响。

每个 RVC 音色以一个模型目录存在：

```text
rvc-backend/models/<model_id>/
├── <model_id>.onnx
├── config.json
└── <model_id>.index     # 可选：检索增强索引
```

当前已部署的音色模型 ID：

```text
de_narrator
```

## 启动 Daemon

在仓库根目录启动全局状态 daemon：

```bash
./build/state/mozart_stated ./rvc-backend/config.yaml
```

正常启动应看到以下关键日志：

```text
RVC config: generator=real, hubert=real, rmvpe=real
ONNX engine loaded: de_narrator.onnx
HuBERT engine loaded
RMVPE engine loaded
HTTP API server listening on 0.0.0.0:18080
state daemon started; initial mode is IDLE
```

另开终端验证服务、真实模型与模型列表：

```bash
curl http://127.0.0.1:18080/api/health
curl http://127.0.0.1:18080/api/status
curl http://127.0.0.1:18080/api/models
```

`/api/status` 应至少包含：

```json
{
  "pipeline_mode": "real",
  "active_model_id": "de_narrator"
}
```

## 启动前端

在第二个终端中执行：

```bash
cd frontend
source ~/.nvm/nvm.sh
npm run dev
```

浏览器打开：

```text
http://127.0.0.1:5173/
```

Vite 会将浏览器的 `/api/...` 请求代理到：

```text
http://127.0.0.1:18080
```

若从局域网其他设备访问 Jetson，请使用 Jetson IP：

```text
http://<jetson-ip>:5173/
```

Vite 配置已启用外部主机监听。

## FILE_RVC 页面流程

1. 打开前端页面。
2. 选择 `FILE_RVC` 模式。
3. 选择 `de_narrator` 音色模型。
4. 选择要转换的音频文件。
5. 按需设置当前支持的 RVC 参数。
6. 点击开始转换。
7. 在队列和底部终端面板查看任务状态与后端日志。
8. 任务完成后在队列中下载 WAV 输出。

底部终端面板直接展示 daemon 的后端日志：

```text
GET /api/logs
```

它不是浏览器端模拟日志。当前 RVC 参数通过以下接口读取和修改：

```text
GET /api/parameters
PUT /api/parameters
```

## 运行时 API 检查

常用检查命令：

```bash
curl http://127.0.0.1:18080/api/parameters
curl "http://127.0.0.1:18080/api/logs?limit=30"
curl http://127.0.0.1:18080/api/models
```

不通过前端，直接验证 FILE_RVC：

```bash
curl -X POST http://127.0.0.1:18080/api/mode/switch \
  -H "Content-Type: application/json" \
  --data '{"mode":"file_rvc","model_id":"de_narrator"}'

curl -X POST http://127.0.0.1:18080/api/file/convert \
  -F "audio_file=@/path/to/input.wav" \
  -F "model_id=de_narrator"

curl "http://127.0.0.1:18080/api/file/status?job_id=<job_id>"
curl -o converted.wav "http://127.0.0.1:18080/api/file/result?job_id=<job_id>"
```

## 停止服务

在 daemon 所在终端按 `Ctrl+C`，可正常停止后端。

在 Vite 所在终端按 `Ctrl+C`，可停止前端开发服务器。

## 常见问题

| 现象 | 检查方法 |
| --- | --- |
| 页面提示 API 离线 | 执行 `curl http://127.0.0.1:18080/api/health`，确认 daemon 正在运行后重启 Vite。 |
| 前端没有可选模型 | 执行 `curl http://127.0.0.1:18080/api/models`；每个模型目录必须有 `<model_id>.onnx` 和 `config.json`。 |
| `pipeline_mode` 显示 `mock` | 检查配置中全部 `rvc.mock.*` 都为 `false`，然后重启 daemon。 |
| 文件任务失败 | 执行 `curl "http://127.0.0.1:18080/api/logs?limit=50"`；相同日志会显示在前端终端面板。 |
| Daemon 无法绑定 18080 端口 | 使用 `ss -ltnp 'sport = :18080'` 找到旧进程，停止后重新启动 daemon。 |
| 音频无法解码 | 确认 `ffmpeg -version` 可以运行，并上传支持的音频格式。 |
| 修改参数时返回 `409` | 当前有 FILE_RVC 任务正在处理；等待完成或取消任务后再修改参数。 |
