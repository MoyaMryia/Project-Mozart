// mozart_post.h
// C ABI for the post-processing side to consume the preprocessing output stream.
// Provided by the Mozart preprocessing library (Rust/C). The post process links
// against mozart_pre.so / mozart_pre.dll, or dlopens it at runtime.
//
// The contract is symmetric to mozart_pre_process (push side):
//   - mozart_post_attach: open the shared ring produced by preprocessing
//   - mozart_post_poll:   pull one frame (16kHz/mono/f32/320 samples) + meta
//   - mozart_post_detach: release handle
//
// This header is consumed both by C++ (this repo) and is the canonical spec
// the Rust side must implement.
#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct mozart_post mozart_post;

typedef struct {
    uint64_t pts_ns;
    uint32_t frame_idx;
    uint8_t  vad_flag;
    uint8_t  energy_db;
    uint8_t  conf;
    uint8_t  segment_id;
} mozart_frame_meta;  // 12 bytes, packed

// Open a reader on the named shared ring. Returns NULL on failure.
// ring_name must be a NUL-terminated UTF-8 string.
mozart_post* mozart_post_attach(const char* ring_name);

// Poll one frame. Returns:
//   1  -> one frame written to out_pcm[320] and *out_meta
//   0  -> timeout, no frame available
//  -1  -> fatal error (caller should detach and stop)
// out_pcm must point to at least 320 floats (16kHz * 20ms).
int mozart_post_poll(mozart_post* handle,
                     float* out_pcm,
                     mozart_frame_meta* out_meta,
                     int timeout_us);

// Release the reader handle. Safe to call with NULL.
void mozart_post_detach(mozart_post* handle);

#ifdef __cplusplus
} // extern "C"
#endif