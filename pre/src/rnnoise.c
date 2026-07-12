// rnnoise.c — RNNoise denoiser stage wrapper.
// ============================================================================
// Wraps xiph.org's RNNoise library (a GRU-based real-time noise suppressor)
// as a mozart_stage_t that can be inserted into the preprocessing pipeline.
//
// RNNoise operates at 48 kHz / 10 ms frames (480 samples) internally.
// The wrapper accepts the pipeline's 16 kHz / 20 ms contract frames; a
// future revision will handle the 48→16 resampling internally.
//
// Two modes, selected at compile time:
//   MOZART_USE_RNNOISE=1  → links native/rnnoise/; real denoising.
//   (undefined)            → passthrough stub; copies input to output.
#include "mozart/rnnoise.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#ifdef MOZART_USE_RNNOISE
#include "rnnoise.h"          // xiph public header (DenoiseState, RNNModel, …)
#else
// ---- Stub declarations (no external dependency) -----------------------------
typedef struct DenoiseState DenoiseState;
typedef struct RNNModel     RNNModel;

static DenoiseState *rnnoise_create(RNNModel *m)
    { (void)m; return NULL; }
static RNNModel *rnnoise_model_from_filename(const char *p)
    { (void)p; return NULL; }
static void rnnoise_model_free(RNNModel *m)
    { (void)m; }
static void rnnoise_destroy(DenoiseState *s)
    { (void)s; }
static float rnnoise_process_frame(DenoiseState *s, float *o, const float *i)
    { (void)s; memcpy(o, i, MOZART_FRAME_SAMPLES * sizeof(float)); return 0.0f; }
static int   rnnoise_get_frame_size(void)
    { return MOZART_FRAME_SAMPLES; }
#endif

// ---- Per-stage private data ------------------------------------------------
/** Holds the RNNoise state and (optional) custom model handle. */
typedef struct {
    DenoiseState *state;  /**< Live denoiser instance. */
    RNNModel     *model;  /**< Weight model (NULL = default embedded). */
} rnnoise_data_t;

// ---- Stage callbacks -------------------------------------------------------
/**
 * Process one frame through RNNoise.
 *
 * If the denoiser state is available, calls rnnoise_process_frame() for
 * actual noise reduction.  Otherwise falls back to a straight memcpy
 * passthrough (stub mode or uninitialised state).
 *
 * @param self   Unused (data is accessed via self->data).
 * @param in     Input PCM buffer (MOZART_FRAME_SAMPLES floats).
 * @param in_len Number of samples (unused, always MOZART_FRAME_SAMPLES).
 * @param out    Output PCM buffer.
 * @param meta   Metadata (unused — RNNoise does not update it).
 * @return       0 always.
 */
static int rnnoise_process(mozart_stage_t *self,
                           const float *in, int in_len,
                           float *out,
                           mozart_frame_meta_t *meta)
{
    rnnoise_data_t *d = (rnnoise_data_t *)self->data;
    (void)in_len;
    (void)meta;

    if (d && d->state) {
        rnnoise_process_frame(d->state, out, in);
    } else {
        memcpy(out, in, MOZART_FRAME_SAMPLES * sizeof(float));
    }
    return 0;
}

/**
 * Destroy the RNNoise stage.
 *
 * Frees the DenoiseState, the custom model (if loaded), and the private
 * data block.  Safe to call with a NULL stage pointer.
 *
 * @param self  Stage to destroy.
 */
static void rnnoise_destroy_stage(mozart_stage_t *self)
{
    if (!self) return;
    rnnoise_data_t *d = (rnnoise_data_t *)self->data;
    if (d) {
        if (d->state) rnnoise_destroy(d->state);
        if (d->model) rnnoise_model_free(d->model);
        free(d);
    }
}

// ---- Public constructor -----------------------------------------------------
/**
 * Create a new RNNoise denoiser stage.
 *
 * Loads the model from a file path (if non-NULL) via rnnoise_model_from_filename(),
 * then creates the DenoiseState via rnnoise_create().  The stage's process
 * callback applies noise reduction to every frame that passes through.
 *
 * In stub mode (MOZART_USE_RNNOISE not defined), model loading and denoising
 * are no-ops — the stage simply copies input to output.
 *
 * @param model_path  Path to a .rnnoise model file, or NULL for the embedded
 *                    default model (compiled from rnnoise_data.c).
 * @return            Heap-allocated stage ready for pipeline insertion,
 *                    or NULL on OOM.
 */
mozart_stage_t *mozart_rnnoise_new(const char *model_path)
{
    rnnoise_data_t *d = calloc(1, sizeof(*d));
    if (!d) return NULL;

    if (model_path) {
        d->model = rnnoise_model_from_filename(model_path);
    }
    d->state = rnnoise_create(d->model);

    mozart_stage_vtable_t vt = {
        .name    = "rnnoise",
        .process = rnnoise_process,
        .destroy = rnnoise_destroy_stage,
    };
    return mozart_stage_new(&vt, d);
}
