// pipewire.h — PipeWire audio capture + virtual source stages.
// ============================================================================
// Two stages for PipeWire integration:
//
//   mozart_pw_capture_new()  — Capture audio from a PipeWire node.
//                               Intended as the first pipeline stage.
//
//   mozart_pw_source_new()   — Expose processed audio as a virtual source
//                               that other apps can select as a microphone.
//                               Intended as the terminal pipeline stage.
//
// Both are currently stubs that store configuration but do not interact
// with libpipewire (pending PipeWire integration).
#ifndef MOZART_PIPEWIRE_H
#define MOZART_PIPEWIRE_H

#include "mozart/stage.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Configuration for the PipeWire capture stage. */
typedef struct {
    const char *capture_node;   /**< PW node name to capture from (NULL = default). */
    const char *source_name;    /**< Virtual source name for other apps.              */
    int         quantum;        /**< Requested quantum in samples (0 = PW default).   */
} mozart_pw_config_t;

mozart_stage_t *mozart_pw_capture_new(const mozart_pw_config_t *cfg);
mozart_stage_t *mozart_pw_source_new (const char *name);

#ifdef __cplusplus
}
#endif

#endif /* MOZART_PIPEWIRE_H */
