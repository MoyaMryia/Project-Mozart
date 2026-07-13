// mozart.h — Public C ABI for the Mozart preprocessing pipeline.
// ============================================================================
// This is the only preprocessing header a downstream consumer needs.
// It defines:
//   - The preprocessing lifecycle and configuration.
//   - Audio contract types imported from the shared IO module.
//   - mozart_pre_config_t  — Pipeline configuration.
//   - mozart_pre_*()       — Lifecycle: init → process → free.
//
#ifndef MOZART_H
#define MOZART_H

#include "mozart/frame_meta.h"

#ifdef __cplusplus
extern "C" {
#endif

// ---- Opaque pipeline handle --------------------------------------------------
typedef struct mozart_pre_ctx mozart_pre_ctx_t;

// ---- Configuration -----------------------------------------------------------
typedef struct {
    const char *rnnoise_model;   /**< Path to .rnnoise model file (NULL = embedded). */
    int         enable_aec;      /**< Enable acoustic echo cancellation (reserved). */
} mozart_pre_config_t;

// ---- Lifecycle ---------------------------------------------------------------
mozart_pre_ctx_t *mozart_pre_init   (const mozart_pre_config_t *cfg);
int               mozart_pre_process(mozart_pre_ctx_t *ctx,
                                     const float *in, int in_samples,
                                     float out[MOZART_INPUT_SAMPLES],
                                     mozart_frame_meta_t *meta);
void              mozart_pre_free   (mozart_pre_ctx_t *ctx);
const char       *mozart_pre_version(void);

#ifdef __cplusplus
}
#endif

#endif /* MOZART_H */
