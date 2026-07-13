// stage.c — Generic stage dispatch using function-pointer vtables.
// ============================================================================
// A "stage" is the fundamental processing unit of the pipeline. Each stage
// wraps a vtable (name + process + destroy) and an opaque data pointer.
// Implementors (RNNoise, AEC, AGC, etc.) fill the vtable and store their
// private state in ->data.
#include "mozart/stage.h"
#include <stdlib.h>

// ---- Construction -----------------------------------------------------------
/**
 * Allocate a new stage from a vtable and private data pointer.
 * The stage takes ownership of `data` — it will be freed by the destroy
 * callback when mozart_stage_destroy() is called.
 *
 * @param vtable  Function-pointer table (at minimum, .process must be set).
 * @param data    Opaque stage-private state (e.g. DenoiseState* + model handle).
 * @return        Heap-allocated stage, or NULL on OOM.
 */
mozart_stage_t *mozart_stage_new(const mozart_stage_vtable_t *vtable, void *data)
{
    mozart_stage_t *s = malloc(sizeof(*s));
    if (!s) return NULL;
    s->vtable = *vtable;
    s->data   = data;
    return s;
}

// ---- Dispatch ---------------------------------------------------------------
/**
 * Invoke a stage's process callback on one audio frame.
 * The input and output buffers must each hold at least `in_len` floats.
 * Metadata (vad_flag, energy, etc.) is read/written in place — stages may
 * update it before passing it downstream.
 *
 * @param s        Stage to dispatch.
 * @param in       Input PCM samples (f32, mono).
 * @param in_len   Number of samples in `in`.
 * @param out      Output PCM buffer (caller-allocated, f32, mono).
 * @param meta     Frame metadata (read-write).
 * @return         0 on success, negative on error.
 */
int mozart_stage_process(mozart_stage_t *s,
                         const float *in, int in_len,
                         float *out,
                         mozart_frame_meta_t *meta)
{
    if (!s || !s->vtable.process) return -1;
    return s->vtable.process(s, in, in_len, out, meta);
}

// ---- Destruction ------------------------------------------------------------
/**
 * Tear down a stage: calls the destroy callback (if set), then frees the
 * stage struct itself.  Safe to call with NULL.
 *
 * @param s  Stage to destroy (may be NULL).
 */
void mozart_stage_destroy(mozart_stage_t *s)
{
    if (!s) return;
    if (s->vtable.destroy) s->vtable.destroy(s);
    free(s);
}
