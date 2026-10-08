# Deploy FILE_RVC

The backend entry point is `mozart_stated`.
The frontend sends API requests through the Vite proxy.
The file worker uses FFmpeg and the RVC pipeline.

The [Chinese version](DEPLOYMENT.zh-CN.md) contains the same procedures.
The [writing rules](../docs/WRITING.md) define the project terms.

## Required components

For the native build, CMake, a C++17 compiler, ALSA development files, yaml-cpp, nlohmann/json, and spdlog are necessary.
FFmpeg is necessary for file conversion.
ONNX Runtime and its development files are necessary for ONNX inference.
The applicable libraries and model engines are necessary for TensorRT inference.
Node.js and npm are necessary for the frontend.

Do a check of the installed programs:

```bash
cmake --version
ffmpeg -version
node --version
npm --version
ldconfig -p | grep onnxruntime
```

If Node.js uses nvm, load nvm first:

```bash
source ~/.nvm/nvm.sh
```

## Build

From the repository root, build the daemon:

```bash
cmake -S . -B build-gpu -DCMAKE_BUILD_TYPE=Release -DUSE_ONNX=ON
cmake --build build-gpu -j4
```

Make sure that the configuration output identifies the necessary inference runtime.
If ONNX Runtime is missing, CMake disables ONNX inference.
Such a build does not prove that real model inference is available.
A `rvc-backend/` build does not produce the daemon.

Build the frontend:

```bash
npm --prefix frontend ci
npm --prefix frontend run build
```

The output directory is `frontend/dist/`.

## Install model assets

An ordinary model uses this directory structure:

```text
rvc-backend/models/<model_id>/
├── <model_id>.onnx
├── <model_id>.engine
└── config.json
```

The `.engine` file is optional.
If that file loads satisfactoryly, the backend uses TensorRT before ONNX Runtime.
The backend does not load retrieval indexes for production inference.

Set the asset paths in `rvc-backend/config.yaml`:

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

Relative file paths use the directory of the configuration file.
They do not use the shell working directory.
The three mock settings must be false for real inference.
Clean file comparisons use `file_rnnoise: false` unless preprocessing is the subject of the test.

## Start the backend

From the repository root, start the daemon:

```bash
./build-gpu/state/mozart_stated rvc-backend/config.yaml
```

The initial mode is `IDLE`.
The factory can select a model from directory order.
It does not guarantee a particular initial model.

In a different terminal, read the service state:

```bash
curl http://127.0.0.1:18080/api/health
curl http://127.0.0.1:18080/api/models
curl http://127.0.0.1:18080/api/status
```

The health response proves only that the HTTP service responds.
Model readiness checks include the selected model, real assets, and a satisfactory conversion.
The `device: cuda` setting does not prove GPU execution.
The engine logs identify the loaded assets and runtime selection.

Select an installed ordinary model explicitly:

```bash
curl -X POST http://127.0.0.1:18080/api/mode/switch \
  -H 'Content-Type: application/json' \
  --data '{"mode":"file_rvc","model_id":"<model_id>"}'
```

Make sure that the response reports `status: active` and the intended `model_id`.

## Start the frontend

In a different terminal, start Vite:

```bash
npm --prefix frontend run dev
```

Open [the local frontend](http://127.0.0.1:5173/).
Vite sends `/api` requests to `http://127.0.0.1:18080`.
The current Vite configuration also accepts local network connections.
Another device uses `http://<jetson-ip>:5173/`.

## Convert a file

1. Select `FILE_RVC` in the frontend.
2. Select an installed ordinary model.
3. Select the input audio file.
4. Start conversion.
5. Wait for the job to report completion.
6. Download the WAV result.

For an API procedure, upload the file:

```bash
curl -X POST http://127.0.0.1:18080/api/file/convert \
  -F 'audio_file=@input.wav' -F 'model_id=<model_id>'
```

Read the returned job state:

```bash
curl 'http://127.0.0.1:18080/api/file/status?job_id=<job_id>'
```

After completion, download the result:

```bash
curl 'http://127.0.0.1:18080/api/file/result?job_id=<job_id>' -o output.wav
```

An active cancellation request waits for the next available cancellation point.
It cannot interrupt the current FFmpeg command or full RVC inference call.
The [API document](../state/API.md) gives the queue and storage limits.

## Use the low-latency profile

The recorded low-latency model is `qiqi-zh-realtime`.
Its directory contains these split Generator assets:

```text
rvc-backend/models/qiqi-zh-realtime/
├── qiqi-zh-realtime-front.onnx
├── qiqi-zh-realtime-front.engine
├── qiqi-zh-realtime-decoder.onnx
├── qiqi-zh-realtime-decoder.engine
└── config.json
```

Set different realtime feature paths:

```yaml
rvc:
  hubert_path: "./assets/hubert/hubert_base_dynamic.onnx"
  rmvpe_path: "./assets/rmvpe/rmvpe_dynamic.onnx"
  realtime_hubert_path: "./assets/hubert/hubert-realtime.onnx"
  realtime_rmvpe_path: "./assets/rmvpe/rmvpe-realtime.onnx"
```

The realtime feature files must have adjacent `.engine` files.
HuBERT uses input `[1,44800]`; RMVPE uses input `[1,128,32]`.
The ordinary feature paths continue to serve file conversion and quality streaming.
A model with only split Generator assets cannot perform ordinary file conversion.

Select the realtime model:

```bash
curl -X POST http://127.0.0.1:18080/api/mode/switch \
  -H 'Content-Type: application/json' \
  --data '{"mode":"rt_rvc","model_id":"qiqi-zh-realtime"}'
curl 'http://127.0.0.1:18080/api/logs?limit=50'
```

Make sure that the log contains this profile name:

```text
upstream realtime (240ms block + 2.5s past + SOLA)
```

The recorded first output latency is approximately 320 ms for this configuration.
The 2.5 s context contains past audio; it adds no equivalent future wait.
Different assets must have their own latency and listening tests.

Mode changes stop the backend UDP worker.
They do not stop an external microphone process.
The [reference speech guide](../tools/REFERENCE_SPEECH.md) gives the TTS deployment procedure.
