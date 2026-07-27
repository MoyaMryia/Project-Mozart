# State Manager HTTP API

`mozart_stated` hosts the `state` control plane. It owns all
mode transitions, real-time worker lifecycle, and the single-consumer
`FILE_RVC` queue. The controller does not receive or mutate audio sample
buffers.

The daemon composes these layers in one process, with the state manager as the
only lifecycle owner:

```text
HTTP API -> StateManagerDaemon -> ModeController
                                  -> RealtimeRvcWorker -> IO C ABI -> AudioWorker
                                  -> FileRvcWorker -> FFmpeg -> preprocessor -> RVC pipeline
```

`rvc_backend` remains a compatibility executable. Deploy `mozart_stated`
from the root `build/` tree for the full daemon architecture.

## Supported Modes

- `idle`: no audio device or worker is active.
- `rt_rvc`: opens the UDP contract stream and starts `AudioWorker`.
- `file_rvc`: closes the real-time stream and consumes one queued job at a time.
- `rt_zero_shot` and `file_zero_shot`: return HTTP `501` until their worker is implemented.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /api/status` | Authoritative mode, pending transition, queue, selected model, and capabilities. |
| `POST /api/mode/switch` | JSON `{ "mode": "file_rvc", "speaker_id": "model_id" }`. A switch away from an active file job is deferred. |
| `POST /api/file/convert` | Multipart `audio_file` and optional `speaker_id`; stores the upload and returns a queued job ID. |
| `GET /api/file/status?job_id=...` | Job state, progress, error, and completed download URL. |
| `DELETE /api/file/cancel?job_id=...` | Removes queued work or requests processing cancellation at the next frame boundary. |
| `GET /api/file/result?job_id=...` | Downloads a completed 48 kHz mono WAV result. |
| `GET /api/models` | Discovers installed RVC models. |
| `POST /api/models/{id}/activate` | Switches model through the controller, never from the HTTP thread directly. |

The file queue has a configurable depth of 50 and a 100 MB request limit.
Temporary files use `storage.temp_dir`; after output writes, the controller
evicts oldest files down to 80% of `storage.max_cache_size_mb` if necessary.
