# FILE_RVC 部署

后端入口为 `mozart_stated`。前端通过 Vite 代理访问 API。
文件工作线程使用 FFmpeg 解码，再执行 RVC 推理。
对应英文说明见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 前置条件

原生构建需要 CMake、C++17 编译器、ALSA 开发文件、yaml-cpp、nlohmann/json 和 spdlog。
文件转换需要 FFmpeg。ONNX 推理需要 ONNX Runtime 及开发文件。
TensorRT 推理需要对应库和引擎资产。前端需要 Node.js 与 npm。

检查已安装程序：

```bash
cmake --version
ffmpeg -version
node --version
npm --version
ldconfig -p | grep onnxruntime
```

如果使用 nvm，先加载环境：

```bash
source ~/.nvm/nvm.sh
```

## 构建

从仓库根目录构建守护进程：

```bash
cmake -S . -B build-gpu -DCMAKE_BUILD_TYPE=Release -DUSE_ONNX=ON
cmake --build build-gpu -j4
```

确认配置输出找到所需推理运行时。当前 CMake 在缺少 ONNX Runtime 时会关闭 ONNX 推理。
此类构建不能证明真实模型可用。独立构建 `rvc-backend/` 不产生守护进程。

构建前端：

```bash
npm --prefix frontend ci
npm --prefix frontend run build
```

产物目录为 `frontend/dist/`。

## 安装模型资产

普通模型目录结构：

```text
rvc-backend/models/<model_id>/
├── <model_id>.onnx
├── <model_id>.engine
└── config.json
```

`.engine` 可选。同名引擎加载成功时，后端优先使用 TensorRT。
生产当前不加载检索索引。

在 `rvc-backend/config.yaml` 设置资产路径：

```yaml
rvc:
  models_dir: "./models"
  hubert_path: "./assets/hubert/hubert_base.onnx"
  rmvpe_path: "./assets/rmvpe/rmvpe.onnx"
  realtime_hubert_path: ""
  realtime_rmvpe_path: ""
  mock:
    generator: false
    hubert: false
    rmvpe: false
storage:
  file_rnnoise: false
```

相对文件路径以配置文件所在目录为基准，不依赖 shell 工作目录。
真实推理必须关闭三个 mock 开关。
干净文件对比使用 `file_rnnoise: false`；仅在专门测试预处理时开启。

## 启动后端

从仓库根目录启动：

```bash
./build-gpu/state/mozart_stated rvc-backend/config.yaml
```

初始模式为 `IDLE`。模型工厂按目录顺序自动选择，不保证目标音色。

另开终端读取服务状态：

```bash
curl http://127.0.0.1:18080/api/health
curl http://127.0.0.1:18080/api/models
curl http://127.0.0.1:18080/api/status
```

健康接口只证明 HTTP 服务响应。模型就绪需要目标模型、真实资产和一次成功转换。
`device: cuda` 不证明实际使用 GPU。加载日志可用于核对资产与运行时选择。

显式选择已经安装的普通模型：

```bash
curl -X POST http://127.0.0.1:18080/api/mode/switch \
  -H 'Content-Type: application/json' \
  --data '{"mode":"file_rvc","model_id":"<model_id>"}'
```

确认响应中的 `status` 为 `active`，`model_id` 与目标一致。

## 启动前端

另开终端启动 Vite：

```bash
npm --prefix frontend run dev
```

打开 [本机界面](http://127.0.0.1:5173/)。
Vite 将 `/api` 请求转发到 `http://127.0.0.1:18080`。
当前配置也接受局域网连接，其他设备使用 `http://<jetson-ip>:5173/`。

## 转换文件

1. 在前端选择 `FILE_RVC`。
2. 选择已经安装的普通模型。
3. 选择输入音频。
4. 启动转换。
5. 等待任务完成。
6. 下载 WAV 结果。

也可通过 API 上传：

```bash
curl -X POST http://127.0.0.1:18080/api/file/convert \
  -F 'audio_file=@input.wav' -F 'model_id=<model_id>'
```

使用响应中的任务 ID 查询：

```bash
curl 'http://127.0.0.1:18080/api/file/status?job_id=<job_id>'
```

完成后下载：

```bash
curl 'http://127.0.0.1:18080/api/file/result?job_id=<job_id>' -o output.wav
```

取消活动任务需要等待可用的取消检查点，无法中断当前 FFmpeg 命令或整段推理调用。
队列与存储约束见 [API 文档](../state/API.md)。

## 使用低延迟配置

已经记录低延迟结果的模型为 `qiqi-zh-realtime`。它的目录包含 split Generator 资产：

```text
rvc-backend/models/qiqi-zh-realtime/
├── qiqi-zh-realtime-front.onnx
├── qiqi-zh-realtime-front.engine
├── qiqi-zh-realtime-decoder.onnx
├── qiqi-zh-realtime-decoder.engine
└── config.json
```

设置独立的实时特征路径：

```yaml
rvc:
  hubert_path: "./assets/hubert/hubert_base_dynamic.onnx"
  rmvpe_path: "./assets/rmvpe/rmvpe_dynamic.onnx"
  realtime_hubert_path: "./assets/hubert/hubert-realtime.onnx"
  realtime_rmvpe_path: "./assets/rmvpe/rmvpe-realtime.onnx"
```

实时特征文件旁需要同名 `.engine`。HuBERT 输入为 `[1,44800]`，RMVPE 输入为 `[1,128,32]`。
普通特征路径继续用于文件转换和 quality 流式推理。仅有 split Generator 的模型不能执行普通文件转换。

选择实时模型：

```bash
curl -X POST http://127.0.0.1:18080/api/mode/switch \
  -H 'Content-Type: application/json' \
  --data '{"mode":"rt_rvc","model_id":"qiqi-zh-realtime"}'
curl 'http://127.0.0.1:18080/api/logs?limit=50'
```

确认日志包含以下配置名称：

```text
upstream realtime (240ms block + 2.5s past + SOLA)
```

该配置已记录首帧出声约 320 ms。2.5 s 上下文来自过去音频，不增加等长的前视等待。
其他资产需要单独测量延迟并试听。

模式切换停止后端 UDP 工作线程，不停止外部麦克风进程。
独立 TTS 部署步骤见 [参考播报说明](../tools/REFERENCE_SPEECH.md)。
