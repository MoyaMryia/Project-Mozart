// rnnoise.h — RNNoise denoiser stage constructor.
// ============================================================================
// Creates a mozart_stage_t that wraps xiph.org's RNNoise real-time noise
// suppressor.  The stage can be inserted into any pipeline position.
//
// Two compile-time modes:
//   MOZART_USE_RNNOISE=1  — Links native/rnnoise/; real GRU-based denoising.
//   (undefined)            — Passthrough stub; copies input → output unchanged.
#ifndef MOZART_RNNOISE_H
#define MOZART_RNNOISE_H

#include "mozart/stage.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Create an RNNoise denoiser stage.
 *
 * @param model_path  Path to a .rnnoise model file, or NULL to use the
 *                    compiled-in default model.
 * @return            Heap-allocated stage.
 */
mozart_stage_t *mozart_rnnoise_new(const char *model_path);

#ifdef __cplusplus
}
#endif

#endif /* MOZART_RNNOISE_H */
