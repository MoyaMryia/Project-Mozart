# Mozart Contract — Post-Process Side Specification

Version: 0.1.0
Scope: the boundary between the **Mozart preprocessing** stage (Rust) and the
**RVC post-processing** stage (C++). The preprocessing side is implemented
elsewhere; this document freezes the contract the post side consumes.

## 1. Audio frame format

| Field | Value |
|---|---|
| Sample rate | 16 kHz |
| Channels | 1 (mono) |
| PCM format | float32, range `[-1.0, 1.0]` |
| Frame duration | 20 ms → 320 samples |

Rationale: preprocessing runs at 48 kHz internally for quality and downsamples
at the contract boundary to 16 kHz (matches HuBERT/ASR/VAD downstream). The RVC
post stage consumes 16 kHz directly; no resampling is needed before feature
extraction. The post stage outputs 48 kHz at its own sink.

## 2. Frame metadata (12 bytes)

```c
#pragma pack(push, 1)
typedef struct {
    uint64_t pts_ns;      // presentation timestamp, nanoseconds since epoch
    uint32_t frame_idx;   // monotonic frame counter from preprocessing
    uint8_t  vad_flag;    // 0 = silence, 1 = voice
    uint8_t  energy_db;   // frame energy dB (quantized 0-255)
    uint8_t  conf;        // denoiser confidence (0-255); post can downweight
    uint8_t  segment_id;  // voice segment id (0 = silence gap)
} mozart_frame_meta;
#pragma pack(pop)
```

`moZart::FrameMeta` (C++, see `include/frame_meta.hpp`) is `#pragma pack(1)`
and `static_assert`ed to 12 bytes. Matches `#[repr(C)]` on the Rust side.

## 3. C ABI (`mozart_post.h`)

```c
typedef struct mozart_post mozart_post;
mozart_post* mozart_post_attach(const char* ring_name);
int          mozart_post_poll(mozart_post* handle,
                              float out_pcm[320],
                              mozart_frame_meta* out_meta,
                              int timeout_us);
void         mozart_post_detach(mozart_post* handle);
```

| Function | Returns | Notes |
|---|---|---|
| `attach` | opaque handle or `NULL` | `ring_name` is a NUL-terminated UTF-8 string identifying the shared ring produced by preprocessing |
| `poll` | `1` OK, `0` timeout, `-1` fatal | blocks up to `timeout_us` for one frame; fills `out_pcm[320]` and `*out_meta` |
| `detach` | — | safe with `NULL` |

## 4. Transport / binding

| Mode | Description |
|---|---|
| **shared ring + C ABI** (default) | Preprocessing publishes a lock-free SPSC ring in shared memory; the post side `dlopen`s `libmozart_pre.so` and calls `mozart_post_attach` |
| **pipeWire virtual stream** (optional) | If metadata can be sidecar-bundled; only considered if ring mode proves impractical |

The C++ side wraps the ABI through `MozartRingSource` (see
`include/contract/mozart_ring_source.hpp`). The library path is searched in
order: `libmozart_pre.so`, `mozart_pre.so`, `mozart_pre`. Missing symbols or
attach failure raise `std::runtime_error` with a clear message.

## 5. Post-stage behavior contract

1. **VAD short-circuit**: when `meta.vad_flag == 0`, the post stage skips
   inference and emits silence at the 48 kHz output. Avoids GPU cost and
   prevents hallucinated speech on silent frames.
2. **Segment state reset**: when `meta.segment_id` changes between consecutive
   frames, the post stage calls `RVCPipeline::reset_state()` so streaming
   buffers (overlaps, hidden states) reinitialize at the new utterance.
3. **Confidence weighting**: low `meta.conf` frames may be down-weighted or
   routed to a pass-through path. Left to tele implementation; default keeps
   the normal path.
4. **Causality**: the post stage keeps the same causal constraint as the
   preprocessor and never looks ahead across frames.

## 6. Frame counts (reference)

| Rate | Frame size | Frame samples |
|---|---|---|
| 16 kHz | 20 ms | 320 (contract input) |
| 48 kHz | 20 ms | 960 (RVC post output) |

Constants:
- `mozart::kContractSamples = 320` (contract input)
- `mozart::kOutputSamples = 960` (post output)
- `mozart::kOutputRate = 48000`

## 7. End-to-end latency budget (RVC post side)

| Stage | Budget |
|---|---|
| Contract poll | ~0 (timeout-managed) |
| VAD short-circuit overhead | <0.05 ms |
| RVC inference (HuBERT + RMVPE + Generator) | TBD on Orin Nano |
| Output sink write | <1 ms |

Phase 2 will measure the RVC inference box and report it as a baseline.