# State manager HTTP API

`mozart_stated` gives the HTTP control service.
`ModeController` owns RVC mode changes, realtime workers, and the file queue.
The controller does not process PCM samples.

```text
HTTP -> StateManagerDaemon -> ModeController
                              -> RealtimeRvcWorker -> UDP IO -> AudioWorker
                              -> FileRvcWorker -> FFmpeg -> RVC
```

The [deployment guide](../frontend/DEPLOYMENT.md) gives the build and start procedures.
The default HTTP port is 18080.
The tables use the `/api` prefix; only some routes also accept an alias without this prefix.

## Modes

| Mode | Current behavior |
| --- | --- |
| `idle` | Stops RVC workers; loaded engines can stay in memory. |
| `rt_rvc` | Opens the UDP stream and starts the realtime worker. |
| `file_rvc` | Closes the realtime stream and processes one file job at a time. |
| `rt_zero_shot` / `file_zero_shot` | Returns HTTP 501; these workers are missing. |

A change away from an active file job uses a pending transition slot.
The controller applies that transition after the job ends.
Later requests can replace the pending transition.
A model change during an active file job returns a busy response.
Independent captions and reference speech can continue in any RVC mode.

The `rvc.enabled: false` setting prevents RVC engine loads and RVC job admission.
The `state/translated-speech.yaml` profile uses this setting.
The native daemon does not own external microphone processes.
An RVC mode change does not stop their capture.

## Model selection

A mode request accepts `model_id`.
The older `speaker_id` field is also an alias for a model ID.
It is not the numeric speaker tensor inside a Generator.

```json
{"mode":"file_rvc","model_id":"de_narrator"}
```

The factory selects an initial model from directory order.
It stops the search even if `load_model()` returns false.
An explicit model selection and a satisfactory inference are necessary deployment checks.

The realtime worker selects the upstream profile when the split Generator and fixed feature assets satisfy its input contract.
This profile uses 240 ms blocks and 2.5 s of past context.
Ordinary models can use quality or legacy streaming when realtime assets are missing.
Realtime assets are necessary for a model with only split Generator assets.
It cannot perform ordinary file conversion.

## Service and control routes

| Request | Response or action |
| --- | --- |
| `GET /api/health` | Returns `{"status":"ok"}`. This is a liveness response. |
| `GET /api/status` | Returns the mode, pending transition, queue, model, capabilities, and active worker statistics. |
| `GET /api/monitor` | Returns CPU, memory, GPU, and PipeWire measurements. |
| `GET /api/logs` | Returns the backend log buffer. |
| `DELETE /api/logs` | Clears that buffer. |
| `POST /api/mode/switch` | Accepts the mode and optional model ID. |
| `POST /api/realtime/routing` | Accepts `mic_muted` and `bypass` Boolean fields. |
| `GET /api/models` | Lists discovered models. |
| `POST /api/models/{id}/activate` | Requests a model change through the controller. |

The controller keeps the routing settings after mode changes.
The controller accepts them even when no realtime worker is active.
Mute produces silent output without inference.
Bypass resamples the input from 16 kHz to 48 kHz without inference.
These controls do not close the physical microphone.

The status statistics include `latency`, `stream`, `bypass`, and `vad`.
The `realtime` object gives the routing settings.
A real pipeline label or a CUDA request does not prove satisfactory GPU execution.
The API does not yet give full information about the actual runtime and asset selection.

## File routes

| Request | Response or action |
| --- | --- |
| `POST /api/file/convert` | Accepts multipart `audio_file` and optional `model_id`; returns a queued job ID. |
| `GET /api/file/status?job_id=...` | Returns state, progress, error, and the completed download URL. |
| `GET /api/file/result?job_id=...` | Downloads a completed 48 kHz mono WAV. |
| `DELETE /api/file/cancel?job_id=...` | Cancels queued work or sets the active cancellation flag. |
| `POST /api/file/pause` | Stops queue consumption after the active job. |
| `POST /api/file/resume` | Starts queue consumption again in file mode. |
| `DELETE /api/file/finished` | Removes terminal jobs and their files. |
| `DELETE /api/file/job?job_id=...` | Removes one inactive job and its files. |

Uploads also accept the `speaker_id` model alias.
A status request must include `job_id`; it does not list the full queue.
The default queue depth is 50 unfinished jobs.
The depth includes `queued`, `processing`, and `cancelling` jobs.
Terminal history does not consume queue capacity.
The returned `queue_position` counts unfinished jobs from one.

The request reader allows approximately 110 MiB, including HTTP and multipart data.
Its error text calls this a 100 MB limit.
This limit is not a decoded audio duration or memory limit.
The [audit report](../reports/repository-audit-20261008/RESULTS.md) records this mismatch.

Active cancellation changes the state to `cancelling`.
The current FFmpeg commands and full inference calls cannot stop at a frame boundary.
The worker reads the flag at the next available cancellation point.
The `cancelling` state continues to own the worker and pipeline.
Daemon shutdown can also wait for these operations.

Temporary files use `storage.temp_dir`.
After a job ends, cache cleanup aims for 80% of `storage.max_cache_size_mb` when the upper limit is exceeded.
Files of unfinished jobs stay protected.
This target is not a hard disk quota.
The queue and terminal history use memory; they do not survive a restart.
Terminal history has no independent count limit.

## Captions, parameters, and speech

| Request | Response or action |
| --- | --- |
| `GET /api/subtitles` | Sends subtitle JSONL records as Server-Sent Events (SSE). |
| `GET /api/parameters` / `PUT /api/parameters` | Reads or changes RVC parameters. |
| `POST /api/parameters/reset` | Restores the effective defaults. |
| `GET /api/presets` / `POST /api/presets` | Lists or saves parameter presets. |
| `DELETE /api/presets/{id}` | Deletes a preset. |
| `/api/voices` and `/api/speech/*` | Proxies requests to the independent reference speech service. |

Subtitles use `MOZART_SUBTITLES_JSONL`, with default path `/tmp/opencode/subtitles.jsonl`.
The reader reopens the file after replacement or truncation.
It waits for incomplete JSONL lines to finish.
The first connection starts at the file end; it does not replay earlier captions.
Each SSE connection uses a thread, with no configured connection limit.

For each utterance, the bridge sends complete records with the same `utterance_id`:

| Stage | Record content |
| --- | --- |
| Partial ASR | Source text with `final: false` and `translation_status: recognizing`. |
| Final ASR | Source text with `final: true` and `translation_status: pending`. |
| Translation | Result with status `completed`, `failed`, or `skipped`. |
| Speech request | Request result or error, if translated text was submitted for speech. |
| Optional ASR refinement | `refined_zh` and `refinement_status`, without another translation or speech request. |

Use `utterance_id` to update the same caption. Accept only a newer revision.
Treat a missing revision as `0` for older producers.
The bridge increases the revision for each update. Stage numbers are not fixed revision values.
Count utterances by ID, not by JSONL line count.
The `final` field marks final ASR text. It does not mean translation or speech has finished.
One background worker processes translation and speech requests in order.
The default coverage policy stores waiting final utterances in a disk queue.
It keeps only recent caption records in memory.
With `--delivery-policy realtime`, the queue holds four waiting final utterances and one active utterance.
For that policy, a full queue produces `translation_status: skipped`; source captions continue.
Partial captions do not start translation or speech.
Optional refinement runs after online ASR. The original `zh` remains the translation source.
The frontend shows a different `refined_zh` separately.
If a newer partial caption arrives, the frontend keeps the most recent completed translation in a separate, labeled line.
Refinement audits use `refinement_numeric_audit` and `refinement_polarity_audit`.
With the coverage policy, normal shutdown waits for queued translations.
An interrupted process can resume stored requests with their original utterance IDs.
With the realtime policy, shutdown allows 20 seconds. Remaining translations then receive an explicit skipped status.

Reference speech uses translated text and does not use the Zero-Shot VC mode enum.
A missing speech service returns HTTP 503.
The [speech contract](../tools/REFERENCE_SPEECH.md) gives its routes, disk queues, and delivery policies.

## Current HTTP limits

Ordinary HTTP requests use one serial request handler.
Accepted connections have no request timeout.
An incomplete request can block health, mode, and cancellation requests.
It can also delay daemon shutdown.
The loopback probe in the audit reproduced the health blockage.

## Regression tests

Run the host regressions from the repository root:

```bash
cmake -S rvc-backend -B build/host -DCMAKE_BUILD_TYPE=Debug \
  -DUSE_ONNX=OFF -DUSE_TENSORRT=OFF -DBUILD_TESTS=ON
cmake --build build/host -j2
ctest --test-dir build/host --output-on-failure
```

A C++17 compiler, ALSA development files, FFmpeg, and local socket access are necessary for these tests.
They use test inference for controller and lifecycle cases.
They do not prove neural quality, GPU latency, or physical audio correctness.
