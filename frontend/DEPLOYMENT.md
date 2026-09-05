# FILE_RVC Deployment And Startup

This document starts the current Project Mozart FILE_RVC stack:

```text
Browser -> Vite frontend -> /api proxy -> mozart_stated -> FILE_RVC worker
        -> FFmpeg -> preprocessor -> HuBERT / RMVPE / RVC Generator ONNX
```

The recommended backend entry point is `mozart_stated`. Do not use the legacy
`rvc_backend` executable for normal deployment.

## Prerequisites

From the repository root, the following must exist:

```text
build/state/mozart_stated
rvc-backend/config.yaml
rvc-backend/assets/hubert/hubert_base.onnx
rvc-backend/assets/rmvpe/rmvpe.onnx
rvc-backend/models/de_narrator/de_narrator.onnx
rvc-backend/models/de_narrator/config.json
```

Check the ONNX Runtime shared library:

```bash
ldconfig -p | grep onnxruntime
```

Check FFmpeg:

```bash
ffmpeg -version
```

Load Node through nvm before running frontend commands:

```bash
source ~/.nvm/nvm.sh
node --version
npm --version
```

## Build

Build the daemon and all required native components from the repository root:

```bash
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build -j4
```

Build the frontend:

```bash
cd frontend
source ~/.nvm/nvm.sh
npm install
npm run build
```

The frontend production bundle is created in `frontend/dist/`.

## Real ONNX Configuration

`rvc-backend/config.yaml` controls the daemon. For real FILE_RVC conversion,
all component mocks must remain disabled:

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

These paths are resolved relative to `rvc-backend/config.yaml`, not the shell
working directory. A model is a directory under `rvc-backend/models/`:

```text
rvc-backend/models/<model_id>/
├── <model_id>.onnx
├── config.json
└── <model_id>.index     # optional retrieval index
```

For example, the installed model ID is `de_narrator`.

## Start The Daemon

From the repository root, start the global state daemon:

```bash
./build/state/mozart_stated ./rvc-backend/config.yaml
```

Expected startup messages include:

```text
RVC config: generator=real, hubert=real, rmvpe=real
ONNX engine loaded: de_narrator.onnx
HuBERT engine loaded
RMVPE engine loaded
HTTP API server listening on 0.0.0.0:18080
state daemon started; initial mode is IDLE
```

Verify health and real model status from another terminal:

```bash
curl http://127.0.0.1:18080/api/health
curl http://127.0.0.1:18080/api/status
curl http://127.0.0.1:18080/api/models
```

`/api/status` should report:

```json
{
  "pipeline_mode": "real",
  "active_model_id": "de_narrator"
}
```

## Start The Frontend

In a second terminal:

```bash
cd frontend
source ~/.nvm/nvm.sh
npm run dev
```

Open:

```text
http://127.0.0.1:5173/
```

Vite proxies browser `/api/...` requests to the daemon at
`http://127.0.0.1:18080`.

For another device on the local network, use the Jetson host address and start
Vite with its default host-enabled configuration:

```text
http://<jetson-ip>:5173/
```

## FILE_RVC Workflow

1. Open the frontend.
2. Select `FILE_RVC`.
3. Select model `de_narrator`.
4. Choose an audio file.
5. Set supported RVC parameters if needed.
6. Click the conversion button.
7. Monitor the queue and the terminal panel.
8. Download the completed WAV from the queue.

The terminal panel renders daemon logs from `GET /api/logs`; it is not a
browser-side simulation. The active RVC parameters are read from and written
to `GET`/`PUT /api/parameters`.

## First-Time Realtime Setup

Start with FILE_RVC. Any ordinary model only needs `<model_id>.onnx`,
`config.json`, and the quality HuBERT/RMVPE paths shown above. Realtime is an
optional profile for one selected model; it does not require every voice model
to be exported twice.

The currently validated low-latency model is `qiqi-zh-realtime`. Its model
directory must contain split Generator engines:

```text
rvc-backend/models/qiqi-zh-realtime/
├── qiqi-zh-realtime-front.onnx
├── qiqi-zh-realtime-front.engine
├── qiqi-zh-realtime-decoder.onnx
├── qiqi-zh-realtime-decoder.engine
└── config.json
```

The two shared fixed-shape feature engines are configured separately from the
quality/file assets:

```yaml
rvc:
  hubert_path: "./assets/hubert/hubert_base_dynamic.onnx"
  rmvpe_path: "./assets/rmvpe/rmvpe_dynamic.onnx"
  realtime_hubert_path: "./assets/hubert/hubert-realtime.onnx"
  realtime_rmvpe_path: "./assets/rmvpe/rmvpe-realtime.onnx"
```

The realtime files must have neighboring `.engine` files. Their contracts are
HuBERT `[1,44800]` and RMVPE `[1,128,32]`. Do not replace the quality paths with
these fixed-shape files, or variable-length FILE_RVC jobs can fail.

Switch the running daemon to realtime and verify the startup log:

```bash
curl -X POST http://127.0.0.1:18080/api/mode/switch \
  -H 'Content-Type: application/json' \
  --data '{"mode":"rt_rvc","model_id":"qiqi-zh-realtime"}'
curl "http://127.0.0.1:18080/api/logs?limit=50"
```

Look for `upstream realtime (240ms block + 2.5s past + SOLA)`. The validated
profile produces its first converted audio after about 320 ms. The 2.5 s past
buffer is historical context, not future waiting. Ordinary models without
realtime assets continue with quality/legacy streaming; `qiqi-zh-realtime`
requires its complete realtime asset set and should not be treated as deployed
when validation fails.

## Runtime API Checks

Useful commands during deployment:

```bash
curl http://127.0.0.1:18080/api/parameters
curl "http://127.0.0.1:18080/api/logs?limit=30"
curl http://127.0.0.1:18080/api/models
```

Test FILE_RVC without the frontend:

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

## Stop

Press `Ctrl+C` in the daemon terminal to stop the backend cleanly.

Press `Ctrl+C` in the Vite terminal to stop the development frontend.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Browser reports API offline | Confirm `curl http://127.0.0.1:18080/api/health` succeeds, then restart Vite. |
| Frontend has no model options | Check `curl http://127.0.0.1:18080/api/models`; each model needs `<model_id>.onnx` and `config.json`. |
| `pipeline_mode` is `mock` | Ensure every `rvc.mock.*` value is `false`, then restart the daemon. |
| Job fails | Read `curl "http://127.0.0.1:18080/api/logs?limit=50"`; the same entries appear in the frontend terminal. |
| Daemon cannot bind port 18080 | Find the previous process with `ss -ltnp 'sport = :18080'`, stop it, then restart the daemon. |
| Audio cannot be decoded | Confirm `ffmpeg -version` works and upload a supported audio file. |
| Parameters return `409` | A file job is actively processing. Wait for it to finish or cancel it before changing runtime parameters. |
