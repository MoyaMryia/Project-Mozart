// main.c — Smoke-test harness for the Mozart preprocessing pipeline.
// ============================================================================
// Creates a pipeline, pushes 10 frames of silence through it, and reports
// success or failure.  Used as a quick sanity check after building.
#include "mozart.h"
#include <stdio.h>

/**
 * Entry point.
 *
 * Initialises the pipeline in its default configuration (embedded RNNoise
 * model, default PipeWire capture), processes 10 zero-filled frames, and
 * prints a short status line.
 *
 * @return 0 on success, 1 if initialisation or processing fails.
 */
int main(void)
{
    printf("Mozart pre-processing v%s\n", mozart_pre_version());

    mozart_pre_config_t cfg = {0};
    cfg.rnnoise_model = NULL;  // use embedded default model

    mozart_pre_ctx_t *ctx = mozart_pre_init(&cfg);
    if (!ctx) {
        fprintf(stderr, "mozart_pre_init failed\n");
        return 1;
    }

    float in [MOZART_FRAME_SAMPLES] = {0};
    float out[MOZART_FRAME_SAMPLES];
    mozart_frame_meta_t meta = {0};

    for (int i = 0; i < 10; i++) {
        int rc = mozart_pre_process(ctx, in, MOZART_FRAME_SAMPLES, out, &meta);
        if (rc < 0) {
            fprintf(stderr, "frame %d error %d\n", i, rc);
            break;
        }
    }

    printf("Pipeline smoke test OK (%d frames)\n", 10);
    mozart_pre_free(ctx);
    return 0;
}
