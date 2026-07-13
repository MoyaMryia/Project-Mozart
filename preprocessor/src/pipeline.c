// pipeline.c — Chains stages into a sequential processing graph.
// ============================================================================
// A pipeline holds up to MAX_STAGES stages in order.  When process() is
// called, each stage reads from the output of the previous stage, with
// metadata passed through and updated in-place.
//
// When N > 0 intermediate stages exist between the first and last, a
// scratch buffer is used to avoid aliasing in and out of the same
// stage's buffers.
#include "mozart/pipeline.h"
#include "mozart/stage.h"
#include <stdlib.h>
#include <string.h>

/** Maximum number of stages allowed in a single pipeline. */
#define MAX_STAGES 8

/** Internal pipeline representation (opaque to callers). */
typedef struct mozart_pipeline {
    mozart_stage_t *stages[MAX_STAGES];  /**< Ordered array of stage pointers. */
    int             count;               /**< Number of stages currently added. */
} mozart_pipeline_t;

// ---- Construction -----------------------------------------------------------
/**
 * Create an empty pipeline.
 * Call mozart_pipeline_add_stage() to populate it.
 *
 * @return  Heap-allocated pipeline, or NULL on OOM.
 */
mozart_pipeline_t *mozart_pipeline_new(void)
{
    mozart_pipeline_t *p = malloc(sizeof(*p));
    if (!p) return NULL;
    p->count = 0;
    return p;
}

// ---- Assembly ---------------------------------------------------------------
/**
 * Append a stage to the pipeline.  The pipeline takes ownership — the stage
 * will be destroyed when mozart_pipeline_destroy() is called.
 *
 * @param p      Pipeline to extend.
 * @param stage  Stage to append (must not be NULL).
 */
void mozart_pipeline_add_stage(mozart_pipeline_t *p, mozart_stage_t *stage)
{
    if (!p || !stage || p->count >= MAX_STAGES) return;
    p->stages[p->count++] = stage;
}

// ---- Processing -------------------------------------------------------------
/**
 * Run a single audio frame through every stage in order.
 *
 * - Stages are called sequentially in the order they were added.
 * - Metadata (vad_flag, energy_db, conf, etc.) is forwarded and may be
 *   updated by each stage.
 * - If a pipeline has only one stage, `out` receives the result directly.
 * - For >1 stage, processing uses an internal scratch buffer so that an
 *   intermediate stage's output never aliases its own input.
 *
 * @param p        Fully assembled pipeline.
 * @param in       Input PCM buffer (at least in_len floats).
 * @param in_len   Number of input samples.
 * @param out      Output PCM buffer (caller-allocated, same size as in).
 * @param meta     Frame metadata (read-write, forwarded across stages).
 * @return         0 on success, negative on first stage error.
 */
int mozart_pipeline_process(mozart_pipeline_t *p,
                            const float *in, int in_len,
                            float *out,
                            mozart_frame_meta_t *meta)
{
    if (!p || !in || !out || !meta) return -1;
    if (in_len <= 0 || in_len > MOZART_INPUT_SAMPLES) return -2;

    const float *src = in;
    float *dst = out;
    float scratch[MOZART_INPUT_SAMPLES];

    for (int i = 0; i < p->count; i++) {
        int rc = mozart_stage_process(p->stages[i], src, in_len, dst, meta);
        if (rc < 0) return rc;

        // If another stage follows, copy the current output into scratch
        // so the next stage doesn't read from its own output buffer.
        if (i + 1 < p->count) {
            memcpy(scratch, dst, (size_t)in_len * sizeof(float));
            src = scratch;
        }
    }
    return 0;
}

// ---- Destruction ------------------------------------------------------------
/**
 * Destroy the pipeline and all owned stages.
 * Each stage's destroy callback is invoked in order, followed by free().
 *
 * @param p  Pipeline to destroy (may be NULL).
 */
void mozart_pipeline_destroy(mozart_pipeline_t *p)
{
    if (!p) return;
    for (int i = 0; i < p->count; i++)
        mozart_stage_destroy(p->stages[i]);
    free(p);
}
