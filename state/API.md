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

The factory sorts model IDs and selects the first model that loads.
If a model fails to load, the search continues.
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
| `GET /api/stack/status` | Reads the supervisor status file selected by `MOZART_STACK_STATUS_JSON`. |
| `GET /api/preprocessor` | Returns audio devices, current selections, and preprocessor state. |
| `PUT /api/preprocessor` | Requires both `capture` and `playback` device IDs and invokes the configured restart script. |
| `GET /api/glossary` / `PUT /api/glossary` | Reads or replaces the term list selected by `MOZART_GLOSSARY`. |

Preprocessor updates require both device IDs:

```json
{"capture":"plughw:CARD=MIC,DEV=0","playback":"plughw:CARD=Device,DEV=0"}
```

Invalid device selections receive HTTP 422. Restart failures receive HTTP 502.
In realtime mode, the controller rebuilds the UDP session after a successful device restart.
The `session` response field reports `restarted` or `unchanged`.

The controller keeps the routing settings after mode changes.
The controller accepts them even when no realtime worker is active.
Mute produces silent output without inference.
Bypass resamples the input from 16 kHz to 48 kHz without inference.
These controls do not close the physical microphone.

The status statistics include `latency`, `stream`, `bypass`, and `vad`.
The `realtime` object gives the routing settings.
The `*_backend` field reports the loaded engine: `tensorrt`, `onnx_cuda_ep`, or `onnx_cpu`.
The `*_asset` field reports the loaded file.
The `model` object reports the runtime and asset selection for each engine role.

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

The complete HTTP request limit is 110 MiB. The audio file limit is 100 MiB.
The decoded audio limit is 300 seconds. Longer inputs fail without a truncated result.
Each FFmpeg operation has a 120-second deadline.
The worker passes arguments directly to FFmpeg without a shell.

Active cancellation changes the state to `cancelling`.
The worker terminates and reaps FFmpeg when cancellation or its deadline occurs.
RVC checks cancellation between feature extraction, retrieval, and synthesis stages, and between inference windows.
An active model call returns before the next cancellation check.
The `cancelling` state continues to own the worker and pipeline.
Daemon shutdown can also wait for these operations.

Temporary files use `storage.temp_dir`.
After a job ends, cache cleanup aims for 80% of `storage.max_cache_size_mb` when the upper limit is exceeded.
Uploads use temporary `.upload` files. Queue admission renames these files under the controller lock.
Cache cleanup protects uploads and files of unfinished jobs.
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
Each connection sends the most recent 200 nonempty JSONL records, then sends new records from the saved file position.
Each SSE connection uses a thread. The server accepts at most eight SSE connections.
A heartbeat occurs every second. The reader closes disconnected clients when a send fails.

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
The `final` field marks the final ASR text. Translation and speech finish after it.
One background worker translates queued utterances in order. A separate worker submits speech requests.
The default coverage policy stores waiting final utterances in a disk queue.
It keeps only recent caption records in memory.
With `--delivery-policy realtime`, the queue holds four waiting final utterances and one active utterance.
For that policy, a full queue produces `translation_status: skipped`; source captions continue.
Partial captions do not start translation or speech.
Optional refinement runs after online ASR. The whole-utterance dispatcher waits within a bounded window.
In whole-utterance mode, `translation_source_text` records the selected source before translation normalization.
The `translation_source_reason` field records the decision. The exact model input is in `translation_audit[].translation_source`.
The `translation_source_audit` field records the selected path, changed glossary terms, and recognition conflicts.
See [source selection](../tools/REFERENCE_SPEECH.md#recognition-uncertainty) for the selection rules.
The frontend displays the selected source. It shows a different `refined_zh` as a candidate.
A late refinement does not replace the source of an existing translation.
If a newer partial caption arrives, the frontend keeps the most recent available translation in a separate, labeled line.
If translation checks fail, `en` contains the last available candidate and `translation_status` stays `failed`.
The frontend labels this candidate and keeps it visible when a newer partial caption arrives.

The bridge enqueues speech only when `translation_status` is `completed`.
A separate worker submits speech requests. A slow speech response does not block the next translation.
The `speech_submission_status` field is `queued`, `sending`, `retrying`, `submitted`, `skipped`, or `failed`.
The `speech_submission_attempts` field counts HTTP attempts. Stable-clause parents sum their segment attempts.
The `speech_last_error` field retains the latest delivery error after recovery.

The `speech_retry_at` field contains the next retry time or `null`.
The `speech` field contains an admitted job ID. Admission is separate from synthesis and playback completion.
Delivery failures leave `translation_status` and the translated text unchanged.

A rejected candidate remains visible and receives `speech_skipped_reason`.
The record keeps `translation_error`, `translation_audit`, and any `speech_error`.
Without a candidate, `en` stays empty. The source text and error remain available.
Refinement audits use `refinement_numeric_audit` and `refinement_polarity_audit`.
The whole-utterance dispatcher includes those issues in `translation_source_audit` when it selects the refinement.
An issue sets `speech_skipped_reason: asr_refinement_uncertainty` and prevents translation and speech.
In whole-utterance mode, `translation_queue_wait_ms` measures enqueue to dequeue within one dispatcher instance.
After disk recovery, this field is `null` because the saved monotonic clock belongs to another dispatcher.
The `translation_refinement_wait_ms` field measures refinement waiting and source selection after dequeue.
The refinement window also includes source formatting and queue time; see [selection rules](../tools/REFERENCE_SPEECH.md#recognition-uncertainty).
The `translate_ms` field measures translation and its checks, including retries.
The `translation_attempts` field counts translation audit rounds, including local term repair.
The `translation_after_asr_ms` field measures final ASR emission to translation completion using wall-clock timestamps.
After disk recovery, this field is `null` in both whole-utterance and stable-clause modes.
Stable-clause segments report translation timing without whole-utterance source selection or refinement-wait fields.
With the coverage policy, normal shutdown waits for queued translations and speech deliveries.
An interrupted process can resume stored requests with their original utterance IDs.
With the realtime policy, shutdown allows 20 seconds. Remaining translations then receive an explicit skipped status.

Reference speech uses translated text and does not use the Zero-Shot VC mode enum.
With the independent early-audio runtime, speech status reports `streaming_playback: true`.
For early playback jobs, `chunks` contains audio blocks and each block identifies its `text_chunk_index`.
The `chunk_count` grows until `generation_done` becomes true. The `text_chunk_count` identifies the original text-piece count.
The `text_part_results` field keeps complete synthesis results for each text piece.
Physical playback timestamps describe pipe delivery. Null playback timestamps use the audio duration.
A missing speech service returns HTTP 503.
Speech status includes `last_stop_at`, the persisted cutoff for delayed bridge requests after explicit stop.
It also includes `control_generation`, which increases after each explicit stop.
Session, preparation, and preview requests can send `expected_control_generation`; stale values receive HTTP 409.
The [speech API](../tools/REFERENCE_SPEECH.md#api) defines the request fields and compatibility behavior.
Archived duplicate admissions retain the original job ID and return `archived: true` with `result_available: false`.
The [speech contract](../tools/REFERENCE_SPEECH.md) gives its routes, disk queues, and delivery policies.

## Current HTTP limits

The server accepts at most 16 concurrent connections, including SSE. Extra connections receive HTTP 503.
Request headers have a 5-second deadline and a 16 KiB limit.
Complete requests have a 30-second deadline. Ordinary responses have a 30-second send deadline.
Partial transfers do not extend these deadlines. Active request data has a shared 128 MiB limit.
Invalid Content-Length fields, duplicate lengths, and transfer encoding receive HTTP 400.
The server interrupts active sockets during shutdown. Model loading and active RVC inference can still delay shutdown.
The speech proxy has a separate 5-second deadline.
The [repair report](../reports/repository-audit-20261013/RESULTS.md) gives the regression scope.

## Regression tests

Run the host regressions from the repository root:

```bash
cmake -S rvc-backend -B build/host -DCMAKE_BUILD_TYPE=Debug \
  -DUSE_ONNX=OFF -DUSE_TENSORRT=OFF -DBUILD_TESTS=ON -DMOZART_BUILD_TESTS=ON
cmake --build build/host -j2
ctest --test-dir build/host --output-on-failure
```

A C++17 compiler, ALSA development files, FFmpeg, and local socket access are necessary for these tests.
They use test inference for controller and lifecycle cases.
They do not prove neural quality, GPU latency, or physical audio correctness.
