// rnnoise.c — RNNoise denoiser stage wrapper.
// ============================================================================
// Wraps xiph.org's RNNoise library (a GRU-based real-time noise suppressor)
// as a mozart_stage_t that can be inserted into the preprocessing pipeline.
//
// RNNoise operates at 48 kHz / 10 ms frames (480 samples) internally.
// This stage therefore only accepts native RNNoise frames. Resampling belongs
// outside this stage.
//
// Two modes, selected at compile time:
//   MOZART_USE_RNNOISE=1  → links native/rnnoise/; real denoising.
//   (undefined)            → passthrough stub; copies input to output.
#include "mozart/rnnoise.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#define RNNOISE_FRAME_SAMPLES 480
#define PCM16_SCALE 32768.0f

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
    { (void)s; memcpy(o, i, RNNOISE_FRAME_SAMPLES * sizeof(float)); return 0.0f; }
static int   rnnoise_get_frame_size(void)
    { return RNNOISE_FRAME_SAMPLES; }
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
 * @param in     Input PCM buffer (one RNNoise-native frame).
 * @param in_len Number of input samples.
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
    if (!in || !out || in_len != rnnoise_get_frame_size()) return -2;

    if (d && d->state) {
        float scaled_in[RNNOISE_FRAME_SAMPLES];
        float scaled_out[RNNOISE_FRAME_SAMPLES];
        for (int i = 0; i < in_len; i++) scaled_in[i] = in[i] * PCM16_SCALE;

        float vad_prob = rnnoise_process_frame(d->state, scaled_out, scaled_in);
        for (int i = 0; i < in_len; i++) out[i] = scaled_out[i] / PCM16_SCALE;

        if (meta) {
            if (vad_prob < 0.0f) vad_prob = 0.0f;
            if (vad_prob > 1.0f) vad_prob = 1.0f;
            meta->conf = (uint8_t)(vad_prob * 255.0f + 0.5f);
            meta->vad_flag = vad_prob >= 0.5f;
        }
    } else {
        memcpy(out, in, (size_t)in_len * sizeof(float));
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
#ifdef MOZART_USE_RNNOISE
        if (!d->model) {
            free(d);
            return NULL;
        }
#endif
    }
    d->state = rnnoise_create(d->model);
#ifdef MOZART_USE_RNNOISE
    if (!d->state) {
        if (d->model) rnnoise_model_free(d->model);
        free(d);
        return NULL;
    }
#endif
#ifdef DEBUG
    fprintf(stderr, "[rnnoise] model=%p state=%p\n", (void*)d->model, (void*)d->state);
#endif

    mozart_stage_vtable_t vt = {
        .name    = "rnnoise",
        .process = rnnoise_process,
        .destroy = rnnoise_destroy_stage,
    };
    return mozart_stage_new(&vt, d);
}
