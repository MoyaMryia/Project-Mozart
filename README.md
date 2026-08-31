# Project Mozart

Real-time AI voice changer for **NVIDIA Jetson Orin Nano Super 8GB**.

Plug in a microphone, talk, and the board outputs your voice — noise-reduced and pitch-shifted in real time. A parallel text path transcribes speech, translates it, and displays subtitles. One edge board, two processing paths, zero Python at runtime.

---

## Architecture

```
Microphone/UDP ──► [IO] ──► [Preprocessor C11] ──contract stream──► [RVC Backend C++17] ──► [IO] ──► Speaker/UDP
                          │                                          │
                          │      PC (one-time export)                │
                          │  .pth ──export──► .onnx                  │
                          │                                          │
                          ▼                                          ▼
                  PyTorch (PC)                           ONNX Runtime / TensorRT (Jetson)
                  zero Python · zero PyTorch dependency
```

**Amphibious architecture**: Model export happens once on a PC with PyTorch. The Jetson runs only ONNX Runtime and TensorRT — no fairseq, no torchcrepe, no Python dependency chain.

### Two parallel paths

| Path | Latency target | Components |
|------|---------------|------------|
| **Realtime** | ~2s + inference (Generator T=200 constraint) | Preprocessor → HuBERT → RMVPE → Generator |
| **Text** | 1–2s per sentence | STT (sherpa-onnx) → LLM (Qwen3.5-0.8B) → subtitles |

---

## Project Structure

| Directory | Language | Description |
|-----------|----------|-------------|
| `IO/` | C++17 + C ABI | Unified contract frames, PipeWire/UDP/Mock drivers, SPSC lock-free ring buffer |
| `preprocessor/` | C11 | ALSA capture → HPF → RNNoise full-wet → 3:1 decimation → VAD hysteresis → MZRT UDP |
| `rvc-backend/` | C++17 | ONNX/TensorRT inference, streaming pipeline with crossfade, HTTP API, model hot-switch |
| `state/` | C++17 | Top-level daemon (`mozart_stated`): mode controller, IDLE/RT_RVC/FILE_RVC mutual exclusion |
| `api/` | C++ | Native socket HTTP server — `/health`, `/status`, `/models`, `/file/*`, `/subtitles` (SSE) |
| `monitor/` | C++ | System telemetry: CPU, memory, GPU load, PipeWire status |
| `frontend/` | Vue 3 + TS + Tailwind | Control panel — mode switching, model management, file queue, subtitles |
| `tools/` | Python | ONNX export scripts, STT/TTS services, full-chain demo, benchmarks |
| `rvc-golden/` | Python | PyTorch golden reference for ONNX regression testing |

---

## Quick Start

### 1. Export ONNX models (PC)

```bash
# Base models (one-time)
python tools/export_hubert_onnx.py
python tools/export_rmvpe_onnx.py

# Per-voice model
python tools/export_generator_onnx.py <model>.pth

# Deploy to Jetson
scp *.onnx *.index moyamryia@<jetson-ip>:~/models/
```

### 2. Build preprocessor (Jetson)

```bash
cd preprocessor && make -j6
# Live mode
./build/bin/mozart-pre -d hw:1,0
# Offline mode (no mic needed)
./build/bin/mozart-pre -i input.wav
```

### 3. Build RVC backend (Jetson)

```bash
cd rvc-backend && mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DUSE_ONNX=ON && make -j6
./rvc_backend ../config.yaml
```

### 4. Run production daemon (recommended)

```bash
cd state && mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release && make -j6
./mozart_stated ../config.yaml
```

---

## Inference Pipeline

```
16kHz input
  → RMVPE ONNX          (mel 128×128 → F0)
  → HuBERT ONNX          (audio → [T, 768] features)
  → Index search          (FAISS IVF, KNN1, no FAISS runtime dependency)
  → Generator ONNX/TRT    (feats + pitch + sid → 48kHz audio)
  → protect blending      (mix with original to preserve characteristics)
```

### Streaming architecture

`StreamingRvc` buffers 2 seconds of audio (T=200 hard constraint), runs inference in a dedicated thread, and produces output with 60ms crossfade overlap-add. Discontinuities (frame index jumps, segment changes, PTS gaps) trigger full state reset.

### Measured performance (Jetson Orin Nano Super 8GB)

| Model | Input | FP32 | FP16 | GPU usage |
|-------|-------|------|------|-----------|
| RMVPE | 1.28s mel | 20.9 ms | 9.4 ms | ~0.7% |
| HuBERT | 0.2s audio | 9.2 ms | 4.5 ms | ~2.3% |
| Generator | 2s audio | 89.9 ms | 37.6 ms | ~1.9% |

Full chain: ~98ms / 2s audio ≈ **5% GPU**.

---

## Contract Frames

All inter-component communication uses a single 16-byte metadata header defined in `IO/include/mozart/frame_meta.h`:

```c
typedef struct {
    uint64_t pts_ns;       // presentation timestamp (nanoseconds)
    uint32_t frame_idx;    // monotonic frame number
    uint8_t  vad_flag;     // 0=silence 1=speech
    uint8_t  energy_db;    // energy 0-255
    uint8_t  conf;         // denoise confidence 0-255
    uint8_t  segment_id;   // speech segment ID (0=silence gap)
} mozart_frame_meta_t;     // 16 bytes, static_assert guaranteed
```

| Stage | Sample rate | Frame size | Bytes/frame |
|-------|------------|------------|-------------|
| raw (device → preprocessor) | 48 kHz | 960 samples (20ms) | 3856 B |
| input (preprocessor → backend) | 16 kHz | 320 samples (20ms) | 1296 B |
| output (backend → device) | 48 kHz | 960 samples (20ms) | 3856 B |

UDP packets carry MZRT magic (`0x4D5A5254`) + 20-byte header + PCM payload. Input packets are 1300 bytes (under MTU 1500).

---

## HTTP API

Native socket implementation, zero web framework. Port 18080.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/status` | GET | Mode, current model, latency stats, bypass count |
| `/monitor` | GET | CPU, memory, GPU load, PipeWire status |
| `/logs` | GET | Backend log ring buffer |
| `/models` | GET | List available voice models |
| `/models/{id}/activate` | POST | Hot-switch voice model (~200ms) |
| `/mode/switch` | POST | Switch operating mode (IDLE/RT_RVC/FILE_RVC) |
| `/file/convert` | POST | Upload audio for batch conversion |
| `/file/status` | GET | Job queue status |
| `/subtitles` | GET | SSE stream of subtitle JSONL |
| `/parameters` | GET/PUT | RVC inference parameters |
| `/presets` | GET | Saved parameter presets |

---

## Configuration

`rvc-backend/config.yaml`:

```yaml
rvc:
  models_dir: "./models"
  hubert_path: "./assets/hubert/hubert_base.onnx"
  rmvpe_path: "./assets/rmvpe/rmvpe.onnx"
  f0_method: "rmvpe"
  pitch_shift: 0
  index_rate: 0.75
  protect: 0.33
  device: "cuda"

network:
  audio:
    port: 18000      # UDP contract stream
  control:
    port: 18080      # HTTP API
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Audio I/O | PipeWire / UDP / ALSA, SPSC lock-free ring, MZRT protocol |
| Preprocessing | RNNoise (xiph), C11, ARM NEON optimized |
| Feature extraction | HuBERT ONNX (768-dim), RMVPE ONNX |
| Synthesis | Generator ONNX / TensorRT, per-voice model |
| Orchestration | C++17 state machine, strong mutual exclusion |
| Control plane | HTTP + SSE, native socket |
| Frontend | Vue 3 + Vite + TypeScript + Tailwind CSS |
| Export tools | Python (fairseq, torchcrepe) |

---

## State Machine

Strong mutual exclusion — only one mode active at a time (8GB shared memory constraint):

| State | Description |
|-------|-------------|
| `IDLE` | No processing, resources released |
| `RT_RVC` | Realtime voice conversion |
| `FILE_RVC` | Batch file conversion (queue, serial) |

Transitions: realtime modes interrupt immediately; file mode waits for current job to complete. Same-category switch (RT_RVC ↔ FILE_RVC) reuses loaded models (~200ms). Cross-category unloads and reloads (~3–6s).

---

## Contributing

- Do **not** `pip install fairseq torchcrepe` on Jetson
- Do **not** commit `.pth` model files (export to ONNX first)
- Do **not** commit `build/` directories
- New documentation goes in the relevant module directory
- New voice models: export ONNX on PC → deploy to Jetson
- RVC quality regression: run Golden Model → compare ONNX → then test in backend

See [AGENTS.md](AGENTS.md) for the debugging workflow and [DESIGN.md](DESIGN.md) for the full system design.

---

## Documentation

| Document | Content |
|----------|---------|
| [DESIGN.md](DESIGN.md) | System design, architecture, contracts, implementation roadmap |
| [TARGET.md](TARGET.md) | Product vision and delivery goals |
| [TODO.md](TODO.md) | Active tasks with measured benchmarks |
| [AGENTS.md](AGENTS.md) | RVC debugging workflow |
| [state/README.md](state/README.md) | State machine, control/data plane separation |
| [state/API.md](state/API.md) | HTTP API specification |
| [rvc-backend/RVC_BACKEND.md](rvc-backend/RVC_BACKEND.md) | Backend development guide |
| [preprocessor/README.md](preprocessor/README.md) | Preprocessor development |
| [frontend/DEPLOYMENT.md](frontend/DEPLOYMENT.md) | Frontend build and deployment |
| [rvc-golden/README.md](rvc-golden/README.md) | Golden model regression testing |

---

## Related

- [RVC WebUI](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) — PC-side export source
- [RNNoise](https://github.com/xiph/rnnoise) — Denoising upstream
- [ONNX Runtime](https://github.com/microsoft/onnxruntime) — Inference engine
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) — STT/TTS engines
