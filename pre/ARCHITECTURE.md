# Mozart Preprocessing — Architecture & Developer Guide

> Project Mozart · Preprocessing Pipeline v0.1.0
> Pure-C implementation for NVIDIA Jetson Orin Nano
> ARM NEON + DOTPROD + FP16 optimised · RNNoise real-time denoising

---

## 1. Overview

Mozart Preprocessing is the **front half** of the AI Sound Card pipeline. It ingests raw microphone audio, cleans it through a chain of causal processing stages, and emits a **contract-guaranteed** clean stream for downstream AI (ASR, voice changer, speaker ID, etc.).

```
  Raw Mic ──► [ Capture ] ──► [ RNNoise ] ──► … ──► Contract Output
              (PipeWire)       (denoiser)    (future     (16kHz/mono/f32
                                               stages)     + 12B metadata)
```

**Key design decisions:**
- Pure **C11** — no runtime, no allocator magic, embedded-friendly.
- **ARM NEON** vector path for Jetson Orin (Cortex-A78AE).
- **Function-pointer vtable** architecture: every processing unit is a pluggable `mozart_stage_t`.
- **12-byte packed metadata** (FrameMeta) is ABI-stable with the C++ post-processing side.

---

## 2. Directory Layout

```
Mozart/
├── mozart.h                          ← Public C ABI (only header consumers need)
├── Makefile                          ← Standalone build (ARM-optimised)
├── CMakeLists.txt                    ← Alternative CMake build
├── include/mozart/
│   ├── stage.h                       ← Stage vtable interface
│   ├── pipeline.h                    ← Pipeline (ordered stage chain)
│   ├── rnnoise.h                     ← RNNoise denoiser constructor
│   └── pipewire.h                    ← PipeWire capture + virtual source
├── src/
│   ├── stage.c                       ← Generic stage dispatch
│   ├── pipeline.c                    ← Pipeline chain + sequential processing
│   ├── rnnoise.c                     ← RNNoise wrapper (real + stub modes)
│   ├── pipewire.c                    ← PipeWire capture/virtual source (stubs)
│   ├── pre.c                         ← Top-level mozart_pre_*() entry points
│   └── main.c                        ← Smoke-test example
├── native/rnnoise/                   ← xiph.org RNNoise v0.2 source
│   ├── include/rnnoise.h
│   └── src/                          ← denoise, rnn, nnet, kiss_fft, …
├── docs/
│   ├── preprocessing-design.md       ← Original design document
│   └── preprocessing-design-fpga.md  ← FPGA variant roadmap
└── ARCHITECTURE.md                   ← This file
```

---

## 3. Core Types

### 3.1 `mozart_frame_meta_t` — 12-byte per-frame metadata

```c
typedef struct {
    uint64_t pts_ns;       // Presentation timestamp (ns)
    uint32_t frame_idx;    // Monotonic frame counter
    uint8_t  vad_flag;     // 0 = silence, 1 = voice
    uint8_t  energy_db;    // Frame energy dB (0-255)
    uint8_t  conf;         // Denoiser confidence (0-255)
    uint8_t  segment_id;   // Voice segment ID (0 = gap)
} mozart_frame_meta_t;
// __attribute__((packed)) — exactly 12 bytes
```

This struct is shared by-value across the contract boundary. It must stay byte-identical with `ai_voice_changer/include/frame_meta.hpp`.

### 3.2 `mozart_stage_t` — Pluggable processing unit

```c
struct mozart_stage {
    mozart_stage_vtable_t vtable;   // { .name, .process, .destroy }
    void                 *data;     // private state (DenoiseState*, etc.)
};
```

Every audio-module (denoiser, AEC, AGC, VAD) is one stage. The pipeline stores stages in an array and calls `.process(in, out, &meta)` sequentially.

### 3.3 `mozart_pipeline_t` — Ordered stage chain

Holds up to **8 stages** in insertion order. On `process()`, each stage's output feeds the next stage's input. Intermediate scratch buffering prevents aliasing.

### 3.4 `mozart_pre_ctx_t` — Opaque top-level handle

Created by `mozart_pre_init(cfg)`, destroyed by `mozart_pre_free()`. Wraps a pipeline plus configuration.

---

## 4. Contract (Pre → Post boundary)

| Parameter      | Value                          |
|----------------|--------------------------------|
| Sample rate    | 16 000 Hz                      |
| Channels       | 1 (mono)                       |
| Sample format  | float32 PCM, range [-1, 1]     |
| Frame duration | 20 ms                          |
| Frame samples  | 320                            |
| Per-frame meta | 12 bytes (see §3.1)            |
| Transport      | Shared ring buffer or PipeWire virtual stream |

> The pipeline *internally* processes at 48 kHz / 10 ms (480 samples) to preserve high-frequency detail during denoising and AEC, then down-samples to 16 kHz at the output boundary. The current stage wrappers operate at the contract rate; resampling integration is pending.

---

## 5. Build System

### Make (recommended)

```bash
make          # Release build with RNNoise (ARM NEON optimised)
make debug    # Debug build with -g -O0
make stub     # Build without RNNoise (passthrough, no native/ dependency)
make run      # Build and run smoke test
make clean    # Remove build/ artifacts
make help     # Show targets
```

**Compiler flags for Jetson Orin:**

| Flag                              | Purpose                                     |
|-----------------------------------|---------------------------------------------|
| `-march=armv8.2-a+fp16+dotprod`   | FP16 half-precision + DOTPROD int8 dot prod |
| `-mtune=cortex-a78ae`             | Tune for Cortex-A78AE microarchitecture     |
| `-O3 -ffast-math -funroll-loops`  | Aggressive optimisation                     |
| `-flto`                           | Link-time optimisation                      |
| `-fno-fast-math` (rnnoise only)   | Required by libopus heritage code           |

### CMake (alternative)

```bash
cmake -S . -B build -DMOZART_USE_RNNOISE=ON
cmake --build build
```

---

## 6. API Reference

### Public API (`mozart.h`)

| Function               | Description                                      |
|------------------------|--------------------------------------------------|
| `mozart_pre_init()`    | Create pipeline, wire stages, return opaque ctx  |
| `mozart_pre_process()` | Push one audio frame through all stages          |
| `mozart_pre_free()`    | Destroy pipeline and free all resources           |
| `mozart_pre_version()` | Return compile-time version string               |

### Stage Construction

| Constructor              | Module            | Status |
|--------------------------|-------------------|--------|
| `mozart_rnnoise_new()`   | RNNoise denoiser  | Ready  |
| `mozart_pw_capture_new()`| PipeWire capture  | Stub   |
| `mozart_pw_source_new()` | Virtual source    | Stub   |

### Stage Vtable (for implementing new stages)

```c
mozart_stage_vtable_t vt = {
    .name    = "my_module",        // debug label
    .process = my_process_callback, // int (*)(stage*, in, len, out, meta)
    .destroy = my_destroy_callback, // void (*)(stage*), may be NULL
};
mozart_stage_t *s = mozart_stage_new(&vt, my_private_data_ptr);
```

---

## 7. Pipeline Stages (Design Document Reference)

Per `docs/preprocessing-design.md`, the full pipeline has 8 stages. Currently implemented:

| # | Module             | Status        | File              |
|---|--------------------|---------------|-------------------|
| ① | PipeWire I/O       | Stub          | `src/pipewire.c`  |
| ② | Normalisation      | Not started   | (future)          |
| ③ | Transient Suppress | Not started   | (future)          |
| ④ | AEC                | Not started   | (future)          |
| ⑤ | **RNNoise Denoise**| **Ready**     | `src/rnnoise.c`   |
| ⑥ | Light Dereverb     | Not started   | (future)          |
| ⑦ | AGC + Clip Guard   | Not started   | (future)          |
| ⑧ | VAD / Tagging      | Not started   | (future)          |

---

## 8. RNNoise Integration

### Build modes

| Mode               | `MOZART_USE_RNNOISE` | Behaviour                                 |
|--------------------|----------------------|-------------------------------------------|
| Real denoising     | `1` (default)        | Links `native/rnnoise/`; GRU-based NR     |
| Passthrough stub   | undefined            | Copies input → output unchanged            |

### RNNoise source tree

```
native/rnnoise/
├── include/rnnoise.h              ← Public API (DenoiseState, RNNModel)
├── src/
│   ├── denoise.c / rnn.c / nnet.c ← Core loop
│   ├── kiss_fft.c / pitch.c       ← FFT + pitch analysis
│   ├── celt_lpc.c                 ← LPC coefficients
│   ├── nnet_default.c             ← Default model weights loader
│   ├── rnnoise_data.c /.h         ← Embedded model weights (downloaded)
│   ├── rnnoise_data_little.c /.h  ← Smaller model variant
│   ├── vec_neon.h                 ← ARM NEON vector intrinsics path
│   ├── vec_avx.h                  ← x86 AVX path (not compiled on ARM)
│   ├── vec.h                      ← Architecture dispatch header
│   └── x86/                       ← x86 macros (harmless on ARM)
├── COPYING                        ← BSD-3 license
└── AUTHORS
```

### ARM NEON verification

Compile-time: `__ARM_NEON` is defined by `-march=armv8.2-a`. The dispatch in `vec.h` selects `vec_neon.h`:

```c
#elif (defined(__ARM_NEON__) || defined(__ARM_NEON)) && !defined(DISABLE_NEON)
#include "vec_neon.h"
```

At runtime, RNNoise's GRU inference uses NEON SIMD for matrix-vector multiplies, pitch correlation, and LPC analysis.

---

## 9. Adding a New Stage

1. **Create a header** in `include/mozart/` declaring the constructor:
   ```c
   mozart_stage_t *mozart_my_new(const my_config_t *cfg);
   ```

2. **Implement in `src/`** with three parts:
   - Private data struct (holds state, handles, buffers).
   - `process()` callback — signature matches `mozart_stage_process_fn`.
   - `destroy()` callback — frees private resources.
   - Constructor that calls `mozart_stage_new(&vt, data)`.

3. **Wire into `src/pre.c`**:
   ```c
   mozart_stage_t *my = mozart_my_new(&my_cfg);
   mozart_pipeline_add_stage(ctx->pipeline, my);
   ```

4. **Add to Makefile** `MOZART_SRCS`.

---

## 10. Debugging & Profiling

### Smoke test (built-in)

```bash
make run
# Output: "Mozart pre-processing v0.1.0\nPipeline smoke test OK (10 frames)"
```

### Latency measurement (future)

The design document specifies an end-to-end budget of ≤ 5 ms. Once PipeWire capture is live, trace each stage's wall-clock time with `clock_gettime(CLOCK_MONOTONIC, …)` inside `mozart_pipeline_process()`.

### Instrumentation tips

- Use `perf record` / `perf report` on Jetson for hotspot analysis.
- RNNoise inner loops live in `src/rnn.c` (GRU cell) and `src/nnet.c` (dense layers).
- Monitor PipeWire quantum with `pw-top`.

---

## 11. Platform: Jetson Orin Nano

| Item       | Detail                                                |
|------------|-------------------------------------------------------|
| CPU        | 6 × Cortex-A78AE @ 1.728 GHz                          |
| ISA        | ARMv8.2-A + NEON + FP16 + DOTPROD                     |
| Memory     | 7.4 GiB LPDDR5                                       |
| GPU        | Ampere SM 8.7 (not used for preprocessing; reserved for post) |
| PipeWire   | 1.0.5, `module-rt` loaded, quantum=1024               |
| Compiler   | GCC 13.3                                              |
| OS         | Linux 6.8.12-tegra, PREEMPT (non-RT)                  |

---

## 12. Design Document Reference

Full design rationale is in `docs/preprocessing-design.md`. Key architectural commitments:

1. **Causal only** — all stages depend only on past and current samples.
2. **Streaming** — no offline/batch processing; one frame in, one frame out.
3. **Userspace only** — runs as a regular process, not a kernel module.
4. **Contract boundary** — output is always 16kHz/mono/f32/20ms + 12B meta.

---

## 13. License

- **Mozart preprocessing** (`src/`, `include/`) — TBD
- **RNNoise** (`native/rnnoise/`) — BSD 3-Clause (xiph.org, Copyright 2017-2024)

---

*Generated 2026-07-12 · Mozart Preprocessing Pipeline v0.1.0*
