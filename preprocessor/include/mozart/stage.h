// stage.h — Single-processing-stage interface.
// ============================================================================
// A "stage" is the fundamental processing unit.  It consists of:
//
//   vtable  — { name, process(), destroy() } function pointers.
//   data    — Opaque pointer to stage-private state.
//
// Implementors (rnnoise, pipewire, AEC, AGC, VAD, …) fill the vtable
// and store their private state in ->data.  The pipeline calls
// process() on each stage in order, forwarding metadata.
//
// Example vtable:
//   mozart_stage_vtable_t vt = {
//       .name    = "my_stage",
//       .process = my_process_fn,
//       .destroy = my_destroy_fn,
//   };
//   mozart_stage_t *s = mozart_stage_new(&vt, my_private_data);
#ifndef MOZART_STAGE_H
#define MOZART_STAGE_H

#include "mozart.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Opaque stage handle (defined in stage.c). */
typedef struct mozart_stage mozart_stage_t;

/** Prototype for per-frame processing.  Returns 0 on success, <0 on error. */
typedef int (*mozart_stage_process_fn)(mozart_stage_t *self,
                                       const float *in, int in_len,
                                       float *out,
                                       mozart_frame_meta_t *meta);

/** Prototype for stage teardown (free private data, close handles, etc.). */
typedef void (*mozart_stage_destroy_fn)(mozart_stage_t *self);

/**
 * Virtual method table for a processing stage.
 *
 * .name     — Human-readable label (for logging / debugging).
 * .process  — Called once per frame (required).
 * .destroy  — Called when the stage is freed (optional, may be NULL).
 */
typedef struct {
    const char              *name;
    mozart_stage_process_fn  process;
    mozart_stage_destroy_fn  destroy;
} mozart_stage_vtable_t;

struct mozart_stage {
    mozart_stage_vtable_t vtable;  /**< Function-pointer table. */
    void                 *data;    /**< Private stage state.    */
};

mozart_stage_t *mozart_stage_new    (const mozart_stage_vtable_t *vtable, void *data);
int             mozart_stage_process(mozart_stage_t *s,
                                     const float *in, int in_len,
                                     float *out,
                                     mozart_frame_meta_t *meta);
void            mozart_stage_destroy(mozart_stage_t *s);

#ifdef __cplusplus
}
#endif

#endif /* MOZART_STAGE_H */
