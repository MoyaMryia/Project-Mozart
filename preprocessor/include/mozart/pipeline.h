// pipeline.h — Ordered chain of processing stages.
// ============================================================================
// A pipeline holds an ordered array of mozart_stage_t pointers and
// processes each audio frame through them sequentially.  The pipeline
// takes ownership of all added stages and destroys them when the
// pipeline is freed.
//
// Typical usage:
//   mozart_pipeline_t *p = mozart_pipeline_new();
//   mozart_pipeline_add_stage(p, mozart_pw_capture_new(&pw_cfg));
//   mozart_pipeline_add_stage(p, mozart_rnnoise_new(NULL));
//   mozart_pipeline_process(p, in, n, out, &meta);
//   mozart_pipeline_destroy(p);
#ifndef MOZART_PIPELINE_H
#define MOZART_PIPELINE_H

#include "mozart.h"
#include "mozart/stage.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct mozart_pipeline mozart_pipeline_t;

mozart_pipeline_t *mozart_pipeline_new(void);
void               mozart_pipeline_add_stage(mozart_pipeline_t *p,
                                              mozart_stage_t *stage);
int                mozart_pipeline_process(mozart_pipeline_t *p,
                                           const float *in, int in_len,
                                           float *out,
                                           mozart_frame_meta_t *meta);
void               mozart_pipeline_destroy(mozart_pipeline_t *p);

#ifdef __cplusplus
}
#endif

#endif /* MOZART_PIPELINE_H */
