// pre.c — Top-level mozart_pre_* entry points (public C ABI).
// ============================================================================
// This file implements the four public functions declared in mozart.h:
//
//   mozart_pre_init()     — Create a pipeline and wire up all stages.
//   mozart_pre_process()  — Push one audio frame through the pipeline.
//   mozart_pre_free()     — Tear down the pipeline and free resources.
//   mozart_pre_version()  — Return a compile-time version string.
//
// Callers only need to include mozart.h — all internal types are opaque.
#include "mozart.h"
#include "mozart/pipeline.h"
#include "mozart/rnnoise.h"
#include "mozart/pipewire.h"
#include <stdlib.h>
#include <string.h>

// ---- Opaque context (defined here, hidden from callers) --------------------

/** Holds the assembled pipeline and capture configuration. */
struct mozart_pre_ctx {
    mozart_pipeline_t *pipeline;   /**< Ordered chain of stages. */
    mozart_pw_config_t pw_cfg;     /**< PipeWire capture config (saved for reference). */
};

/** Compile-time version (semver). */
static const char *version_str = "0.1.0";

// ---- Lifecycle --------------------------------------------------------------

/**
 * Initialise the Mozart preprocessing pipeline.
 *
 * Stages are assembled in the order prescribed by the design document:
 *   ① PipeWire capture (I/O layer)
 *   ⑤ RNNoise denoiser
 *
 * Additional stages (normalisation, transient suppression, AEC,
 * dereverberation, AGC, VAD) will be inserted between capture and denoiser
 * as they are implemented.
 *
 * @param cfg  Pipeline configuration (may be NULL for defaults).
 * @return     Opaque context handle, or NULL on allocation failure.
 */
mozart_pre_ctx_t *mozart_pre_init(const mozart_pre_config_t *cfg)
{
    mozart_pre_ctx_t *ctx = calloc(1, sizeof(*ctx));
    if (!ctx) return NULL;

    ctx->pipeline = mozart_pipeline_new();
    if (!ctx->pipeline) {
        free(ctx);
        return NULL;
    }

    // Stage 1: PipeWire capture (I/O layer)
    mozart_pw_config_t pw_cfg = {0};
    if (cfg) {
        pw_cfg.capture_node = cfg->pipewire_node;
        pw_cfg.quantum      = cfg->quantum_samples;
        pw_cfg.source_name  = "mozart_pre";
    }
    mozart_stage_t *cap = mozart_pw_capture_new(&pw_cfg);
    mozart_pipeline_add_stage(ctx->pipeline, cap);

    // Stage 5: RNNoise denoiser
    mozart_stage_t *rn = mozart_rnnoise_new(cfg ? cfg->rnnoise_model : NULL);
    mozart_pipeline_add_stage(ctx->pipeline, rn);

    return ctx;
}

/**
 * Process one audio frame through the entire preprocessing pipeline.
 *
 * The frame flows through every stage in order.  Each stage may read
 * and modify the metadata in place.
 *
 * @param ctx        Pipeline context from mozart_pre_init().
 * @param in         Input PCM buffer (in_samples floats).
 * @param in_samples Number of input samples (must equal MOZART_FRAME_SAMPLES).
 * @param out        Output PCM buffer (caller-allocated, in_samples floats).
 * @param meta       Frame metadata (read-write; initialised by caller).
 * @return           0 on success, negative on processing error.
 */
int mozart_pre_process(mozart_pre_ctx_t *ctx,
                       const float *in, int in_samples,
                       float *out,
                       mozart_frame_meta_t *meta)
{
    if (!ctx || !ctx->pipeline) return -1;
    return mozart_pipeline_process(ctx->pipeline, in, in_samples, out, meta);
}

/**
 * Destroy the pipeline context and release all resources.
 *
 * Calls mozart_pipeline_destroy() for the internal stage chain, then
 * frees the context struct itself.  Safe to call with NULL.
 *
 * @param ctx  Context to destroy (may be NULL).
 */
void mozart_pre_free(mozart_pre_ctx_t *ctx)
{
    if (!ctx) return;
    mozart_pipeline_destroy(ctx->pipeline);
    free(ctx);
}

/**
 * Return the compile-time version string of the preprocessing library.
 *
 * @return  Null-terminated semver string (e.g. "0.1.0").
 */
const char *mozart_pre_version(void)
{
    return version_str;
}
