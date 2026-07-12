// pipewire.c — PipeWire capture + virtual source stages (stubs).
// ============================================================================
// Two stage types are defined:
//
//   mozart_pw_capture_new()  — Captures audio from a PipeWire node.
//                               Intended as the *first* pipeline stage (I/O layer).
//
//   mozart_pw_source_new()   — Registers a virtual audio source that other
//                               applications (OBS, browser, meeting software)
//                               can select as their microphone.
//                               Intended as the *terminal* pipeline stage.
//
// Both are currently stubs: they produce silence / store the node name
// but do not yet interact with libpipewire.
#include "mozart/pipewire.h"
#include <stdlib.h>
#include <string.h>

// ============================================================================
// Capture stage
// ============================================================================

/** Private data for the PipeWire capture stage. */
typedef struct {
    char     capture_node[256];  /**< PW node name to capture from. */
    int      quantum;            /**< Requested quantum in samples (0 = default). */
    uint64_t frame_idx;          /**< Monotonic frame counter for metadata. */
} pw_capture_data_t;

/**
 * Capture one frame from PipeWire (stub).
 *
 * Currently fills the output buffer with silence and bumps the monotonic
 * frame index.  The real implementation will read from a PipeWire stream.
 *
 * @param self    Stage handle (private data accessed via self->data).
 * @param in      Unused (capture has no upstream input).
 * @param in_len  Unused.
 * @param out     Output buffer filled with captured PCM.
 * @param meta    Metadata updated with frame index.
 * @return        0 always.
 */
static int pw_capture_process(mozart_stage_t *self,
                              const float *in, int in_len,
                              float *out,
                              mozart_frame_meta_t *meta)
{
    pw_capture_data_t *d = (pw_capture_data_t *)self->data;
    (void)in;
    (void)in_len;
    (void)self;

    memset(out, 0, MOZART_FRAME_SAMPLES * sizeof(float));
    meta->frame_idx = d->frame_idx++;
    meta->vad_flag  = 0;
    return 0;
}

/**
 * Destroy the capture stage.
 *
 * Frees the private data block.  In the real implementation, this will
 * also close the PipeWire stream and context.
 *
 * @param self  Stage to destroy (may be NULL).
 */
static void pw_capture_destroy(mozart_stage_t *self)
{
    if (!self) return;
    free(self->data);
}

/**
 * Create a PipeWire audio capture stage.
 *
 * Allocates a capture stage that will pull audio from the specified
 * PipeWire node.  When PipeWire integration is complete, this will open
 * a pw_stream in capture mode.
 *
 * @param cfg  Capture configuration (node name, quantum).
 * @return     Heap-allocated stage, or NULL on OOM.
 */
mozart_stage_t *mozart_pw_capture_new(const mozart_pw_config_t *cfg)
{
    pw_capture_data_t *d = calloc(1, sizeof(*d));
    if (!d) return NULL;
    if (cfg && cfg->capture_node)
        strncpy(d->capture_node, cfg->capture_node, sizeof(d->capture_node) - 1);
    if (cfg && cfg->quantum > 0)
        d->quantum = cfg->quantum;

    mozart_stage_vtable_t vt = {
        .name    = "pw_capture",
        .process = pw_capture_process,
        .destroy = pw_capture_destroy,
    };
    return mozart_stage_new(&vt, d);
}

// ============================================================================
// Virtual source stage
// ============================================================================

/** Private data for the PipeWire virtual source stage. */
typedef struct {
    char source_name[256];  /**< Name advertised to other PW clients. */
} pw_source_data_t;

/**
 * Create a PipeWire virtual audio source (stub).
 *
 * Registers a PipeWire stream that other applications can use as an
 * audio input.  The real implementation will create a pw_stream in
 * playback mode and write processed frames to it.
 *
 * @param name  Human-readable source name (e.g. "Mozart Preprocessed Mic").
 * @return      Heap-allocated stage, or NULL on OOM.
 */
mozart_stage_t *mozart_pw_source_new(const char *name)
{
    pw_source_data_t *d = calloc(1, sizeof(*d));
    if (!d) return NULL;
    if (name)
        strncpy(d->source_name, name, sizeof(d->source_name) - 1);

    mozart_stage_vtable_t vt = {
        .name    = "pw_source",
        .process = NULL,   // terminal stage — no downstream processing
        .destroy = NULL,   // no external resources yet
    };
    return mozart_stage_new(&vt, d);
}
