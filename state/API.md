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

`mozart_stated` is the only backend entry point. Deploy it from the root
`build/` tree for the full daemon architecture.

## Supported Modes

- `idle`: no audio device or worker is active.
- `rt_rvc`: opens the UDP contract stream and starts `AudioWorker`.
- `file_rvc`: closes the real-time stream and consumes one queued job at a time.
- `rt_zero_shot` and `file_zero_shot`: return HTTP `501` until their worker is implemented.

`rt_rvc` automatically selects the low-latency upstream realtime profile when
the selected model has split `front`/`decoder` engines and the configured
realtime HuBERT/RMVPE assets pass fixed-shape TensorRT validation. The validated
profile uses a 240 ms block and 2.5 s rolling past context; the past context is
not future buffering. If those assets are absent, the worker falls back to the
quality/legacy streaming profile when that model has a regular Generator.
The split-only `qiqi-zh-realtime` profile requires its realtime assets and is
not considered deployed when validation fails.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /api/status` | Authoritative mode, pending transition, queue, selected model, capabilities, plus `latency` (avg/max ms), `stream` (blocks/resets/overruns), `bypass` (inference/bypass counts), and `vad` stats from the active real-time worker. Also carries `realtime` routing state (`mic_muted` / `bypass`). |
| `POST /api/mode/switch` | JSON `{ "mode": "file_rvc", "speaker_id": "model_id" }`. A switch away from an active file job is deferred. |
| `POST /api/realtime/routing` | JSON `{ "mic_muted": bool, "bypass": bool }`. Mute outputs silent frames (no inference); bypass plays the raw 16 kHz input upsampled to 48 kHz (no inference). Routing state survives mode switches and is reported in `status.realtime`. Requires a running RT_RVC worker. |
| `POST /api/file/convert` | Multipart `audio_file` and optional `speaker_id`; stores the upload and returns a queued job ID. |
| `GET /api/file/status?job_id=...` | Job state, progress, error, and completed download URL. |
| `DELETE /api/file/cancel?job_id=...` | Removes queued work or requests processing cancellation at the next frame boundary. |
| `GET /api/file/result?job_id=...` | Downloads a completed 48 kHz mono WAV result. |
| `GET /api/models` | Discovers installed RVC models. |
| `POST /api/models/{id}/activate` | Switches model through the controller, never from the HTTP thread directly. |

The file queue has a configurable depth of 50 and a 100 MB request limit.
The depth counts `queued`, `processing`, and `cancelling` jobs; terminal history
does not consume capacity. `queue_position` is one-based among unfinished jobs.
`DELETE /api/file/finished` removes only `completed`, `failed`, and `cancelled`
jobs and their files. Queued work is retained.

Temporary files use `storage.temp_dir`; after a job finishes, the controller
evicts oldest unprotected files towards 80% of `storage.max_cache_size_mb`.
Files owned by queued, processing, or cancelling jobs are protected, even when
retaining them keeps the directory above the threshold. This cache target is
not a hard disk quota.

Queue lifecycle regression tests run on a Linux host with CMake, Ninja, a C++17
compiler, ALSA development headers, and FFmpeg:

```bash
cmake -S rvc-backend -B build/host -G Ninja -DCMAKE_BUILD_TYPE=Debug \
  -DUSE_ONNX=OFF -DUSE_TENSORRT=OFF -DBUILD_TESTS=ON
cmake --build build/host --target test_mode_controller -j2
ctest --test-dir build/host -R '^state_' --output-on-failure
```

These tests use the real controller, file worker, and FFmpeg with test-only
inference. They do not validate model quality, GPU latency, or physical audio I/O.
