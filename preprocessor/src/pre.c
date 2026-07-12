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
#include "mozart/rnnoise.h"
#include "mozart/stage.h"
#include <math.h>
#include <stdlib.h>
#include <string.h>

#define RNNOISE_FRAME_SAMPLES 480
#define DECIMATOR_TAPS 33
#define HP_B0  0.99262254f
#define HP_B1 -1.98524509f
#define HP_B2  0.99262254f
#define HP_A1 -1.98519066f
#define HP_A2  0.98529951f

/* 7.2 kHz low-pass, Kaiser window; attenuates content above 16 kHz Nyquist. */
static const float decimator_coeffs[DECIMATOR_TAPS] = {
     4.2914726915e-04f,  1.4895510869e-03f,  1.5189746882e-03f,
    -1.2652814003e-03f, -5.8142304759e-03f, -7.0800492720e-03f,
     0.0000000000e+00f,  1.3328130710e-02f,  2.0914031268e-02f,
     8.9839746223e-03f, -2.2538271381e-02f, -5.0922926864e-02f,
    -4.0588192824e-02f,  3.0283637766e-02f,  1.4611043959e-01f,
     2.5519851986e-01f,  2.9990509072e-01f,  2.5519851986e-01f,
     1.4611043959e-01f,  3.0283637766e-02f, -4.0588192824e-02f,
    -5.0922926864e-02f, -2.2538271381e-02f,  8.9839746223e-03f,
     2.0914031268e-02f,  1.3328130710e-02f,  0.0000000000e+00f,
    -7.0800492720e-03f, -5.8142304759e-03f, -1.2652814003e-03f,
     1.5189746882e-03f,  1.4895510869e-03f,  4.2914726915e-04f
};

// ---- Opaque context (defined here, hidden from callers) --------------------

/** Holds the stateful 48 kHz denoiser and 3:1 decimator. */
struct mozart_pre_ctx {
    mozart_stage_t *rnnoise;
    float           fir_history[DECIMATOR_TAPS];
    int             fir_pos;
    float           hp_x1;
    float           hp_x2;
    float           hp_y1;
    float           hp_y2;
    float           noise_power;
    float           wet;
};

/** Compile-time version (semver). */
static const char *version_str = "0.1.0";

// ---- Lifecycle --------------------------------------------------------------

/**
 * Initialise the Mozart preprocessing pipeline.
 *
 * The caller supplies normalized 48 kHz mono PCM. Each 20 ms input frame is
 * denoised as two native RNNoise frames, then decimated to the 16 kHz contract.
 * Capture remains an I/O concern outside this audio processing API.
 *
 * @param cfg  Pipeline configuration (may be NULL for defaults).
 * @return     Opaque context handle, or NULL on allocation failure.
 */
mozart_pre_ctx_t *mozart_pre_init(const mozart_pre_config_t *cfg)
{
    mozart_pre_ctx_t *ctx = calloc(1, sizeof(*ctx));
    if (!ctx) return NULL;

    ctx->rnnoise = mozart_rnnoise_new(cfg ? cfg->rnnoise_model : NULL);
    if (!ctx->rnnoise) {
        free(ctx);
        return NULL;
    }
    ctx->noise_power = 1e-6f;
    ctx->wet = 0.70f;
    return ctx;
}

static void highpass_80hz(mozart_pre_ctx_t *ctx, const float *in, float *out,
                          int samples)
{
    for (int i = 0; i < samples; i++) {
        float x = in[i];
        float y = HP_B0 * x + HP_B1 * ctx->hp_x1 + HP_B2 * ctx->hp_x2
                  - HP_A1 * ctx->hp_y1 - HP_A2 * ctx->hp_y2;
        ctx->hp_x2 = ctx->hp_x1;
        ctx->hp_x1 = x;
        ctx->hp_y2 = ctx->hp_y1;
        ctx->hp_y1 = y;
        out[i] = y;
    }
}

static float frame_power(const float *samples, int count)
{
    float sum = 0.0f;
    for (int i = 0; i < count; i++) sum += samples[i] * samples[i];
    return sum / count;
}

static float update_wet(mozart_pre_ctx_t *ctx, float power, float vad_prob)
{
    if (vad_prob < 0.35f) {
        float rate = power < ctx->noise_power ? 0.10f : 0.01f;
        ctx->noise_power += rate * (power - ctx->noise_power);
    }

    float snr_db = 10.0f * log10f((power + 1e-12f) /
                                  (ctx->noise_power + 1e-12f));
    float target;
    if (snr_db >= 25.0f) target = 0.45f;
    else if (snr_db >= 15.0f) target = 0.60f;
    else if (snr_db >= 8.0f) target = 0.75f;
    else target = 0.90f;

    if (vad_prob >= 0.5f && target > 0.75f) target = 0.75f;
    ctx->wet += 0.10f * (target - ctx->wet);
    return ctx->wet;
}

static void decimate_3x(mozart_pre_ctx_t *ctx, const float *in, float *out)
{
    int out_pos = 0;
    for (int i = 0; i < MOZART_INPUT_FRAME_SAMPLES; i++) {
        ctx->fir_history[ctx->fir_pos] = in[i];
        ctx->fir_pos = (ctx->fir_pos + 1) % DECIMATOR_TAPS;

        if (i % 3 == 2) {
            float sum = 0.0f;
            int pos = ctx->fir_pos;
            for (int tap = 0; tap < DECIMATOR_TAPS; tap++) {
                pos = pos == 0 ? DECIMATOR_TAPS - 1 : pos - 1;
                sum += decimator_coeffs[tap] * ctx->fir_history[pos];
            }
            out[out_pos++] = sum;
        }
    }
}

/**
 * Process one audio frame through the entire preprocessing pipeline.
 *
 * The frame flows through every stage in order.  Each stage may read
 * and modify the metadata in place.
 *
 * @param ctx        Pipeline context from mozart_pre_init().
 * @param in         Input PCM buffer (in_samples floats).
 * @param in_samples Number of 48 kHz input samples (must be 960).
 * @param out        Output PCM buffer (caller-allocated, 320 floats at 16 kHz).
 * @param meta       Frame metadata (read-write; initialised by caller).
 * @return           0 on success, negative on processing error.
 */
int mozart_pre_process(mozart_pre_ctx_t *ctx,
                       const float *in, int in_samples,
                       float out[MOZART_FRAME_SAMPLES],
                       mozart_frame_meta_t *meta)
{
    if (!ctx || !ctx->rnnoise || !in || !out || !meta) return -1;
    if (in_samples != MOZART_INPUT_FRAME_SAMPLES) return -2;

    float filtered[MOZART_INPUT_FRAME_SAMPLES];
    float denoised[MOZART_INPUT_FRAME_SAMPLES];
    float mixed[MOZART_INPUT_FRAME_SAMPLES];
    unsigned int conf_sum = 0;
    uint8_t vad_any = 0;
    highpass_80hz(ctx, in, filtered, MOZART_INPUT_FRAME_SAMPLES);
    for (int offset = 0; offset < MOZART_INPUT_FRAME_SAMPLES;
         offset += RNNOISE_FRAME_SAMPLES) {
        int rc = mozart_stage_process(ctx->rnnoise, filtered + offset,
                                      RNNOISE_FRAME_SAMPLES, denoised + offset, meta);
        if (rc < 0) return rc;
        float vad_prob = meta->conf / 255.0f;
        float wet = update_wet(ctx,
                               frame_power(filtered + offset, RNNOISE_FRAME_SAMPLES),
                               vad_prob);
        for (int i = offset; i < offset + RNNOISE_FRAME_SAMPLES; i++) {
            mixed[i] = wet * denoised[i] + (1.0f - wet) * filtered[i];
        }
        conf_sum += meta->conf;
        vad_any |= meta->vad_flag;
    }
    meta->conf = (uint8_t)((conf_sum + 1) / 2);
    meta->vad_flag = vad_any;
    decimate_3x(ctx, mixed, out);
    return 0;
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
    mozart_stage_destroy(ctx->rnnoise);
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
