// mozart.h — Public C ABI for the Mozart preprocessing pipeline.
// ============================================================================
// This is the ONLY header a downstream consumer needs to include.
// It defines:
//   - Audio contract constants (sample rate, frame size, channels).
//   - mozart_frame_meta_t  — 12-byte per-frame metadata, ABI-stable.
//   - mozart_pre_config_t  — Pipeline configuration.
//   - mozart_pre_*()       — Lifecycle: init → process → free.
//
// The FrameMeta layout MUST stay in sync with the C++ FrameMeta in the
// ai_voice_changer post-processing project (include/frame_meta.hpp).
#ifndef MOZART_H
#define MOZART_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// ---- Audio contract constants ------------------------------------------------
#define MOZART_SAMPLE_RATE   16000   /**< Output sample rate (Hz).                  */
#define MOZART_CHANNELS      1       /**< Output channel count (mono).              */
#define MOZART_FRAME_MS      20      /**< Frame duration in milliseconds.           */
#define MOZART_FRAME_SAMPLES 320     /**< Samples per frame (rate × ms / 1000).    */

// ---- Per-frame metadata (12 bytes, packed) -----------------------------------
#pragma pack(push, 1)
typedef struct {
    uint64_t pts_ns;       /**< Presentation timestamp, nanoseconds since epoch. */
    uint32_t frame_idx;    /**< Monotonic frame counter from preprocessing.       */
    uint8_t  vad_flag;     /**< Voice activity: 0 = silence, 1 = voice.           */
    uint8_t  energy_db;    /**< Frame energy in dB (0-255 quantized).             */
    uint8_t  conf;         /**< Denoiser confidence 0-255 (post can downweight).  */
    uint8_t  segment_id;   /**< Voice segment id (0 = silence gap).               */
} mozart_frame_meta_t;
#pragma pack(pop)

// ---- Opaque pipeline handle --------------------------------------------------
typedef struct mozart_pre_ctx mozart_pre_ctx_t;

// ---- Configuration -----------------------------------------------------------
typedef struct {
    const char *pipewire_node;   /**< PipeWire capture node (NULL = default mic). */
    const char *rnnoise_model;   /**< Path to .rnnoise model file (NULL = embedded). */
    int         enable_aec;      /**< Enable acoustic echo cancellation (reserved). */
    int         quantum_samples; /**< PipeWire quantum in samples (0 = PW default).  */
} mozart_pre_config_t;

// ---- Lifecycle ---------------------------------------------------------------
mozart_pre_ctx_t *mozart_pre_init   (const mozart_pre_config_t *cfg);
int               mozart_pre_process(mozart_pre_ctx_t *ctx,
                                     const float *in, int in_samples,
                                     float *out,
                                     mozart_frame_meta_t *meta);
void              mozart_pre_free   (mozart_pre_ctx_t *ctx);
const char       *mozart_pre_version(void);

#ifdef __cplusplus
}
#endif

#endif /* MOZART_H */
