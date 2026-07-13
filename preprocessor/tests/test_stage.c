#include "mozart.h"
#include <math.h>
#include <stdio.h>

static int fail(const char *message)
{
    fprintf(stderr, "FAIL: %s\n", message);
    return 1;
}

int main(void)
{
    mozart_pre_ctx_t *ctx = mozart_pre_init(NULL);
    if (!ctx) return fail("mozart_pre_init");

    float input[MOZART_RAW_SAMPLES] = {0};
    float output[MOZART_INPUT_SAMPLES];
    mozart_frame_meta_t meta = {0};

    if (mozart_pre_process(ctx, input, MOZART_RAW_SAMPLES - 1,
                           output, &meta) != -2) {
        mozart_pre_free(ctx);
        return fail("invalid input length was accepted");
    }

    for (int frame = 0; frame < 2; frame++) {
        if (mozart_pre_process(ctx, input, MOZART_RAW_SAMPLES,
                               output, &meta) != 0) {
            mozart_pre_free(ctx);
            return fail("20 ms frame processing");
        }
        for (int i = 0; i < MOZART_INPUT_SAMPLES; i++) {
            if (!isfinite(output[i]) || fabsf(output[i]) > 1e-7f) {
                mozart_pre_free(ctx);
                return fail("silence did not remain silence");
            }
        }
    }

    mozart_pre_free(ctx);

#ifndef MOZART_USE_RNNOISE
    ctx = mozart_pre_init(NULL);
    if (!ctx) return fail("mozart_pre_init for filter test");
    for (int i = 0; i < MOZART_RAW_SAMPLES; i++) input[i] = 0.5f;
    for (int frame = 0; frame < 20; frame++) {
        if (mozart_pre_process(ctx, input, MOZART_RAW_SAMPLES,
                               output, &meta) != 0) {
            mozart_pre_free(ctx);
            return fail("DC filter processing");
        }
    }
    float dc_rms = 0.0f;
    for (int i = 0; i < MOZART_INPUT_SAMPLES; i++) dc_rms += output[i] * output[i];
    dc_rms = sqrtf(dc_rms / MOZART_INPUT_SAMPLES);
    if (dc_rms > 1e-4f) {
        mozart_pre_free(ctx);
        return fail("80 Hz high-pass did not reject DC");
    }
    mozart_pre_free(ctx);

    ctx = mozart_pre_init(NULL);
    if (!ctx) return fail("mozart_pre_init for passband test");
    for (int i = 0; i < MOZART_RAW_SAMPLES; i++)
        input[i] = 0.25f * sinf(2.0f * 3.1415926536f * 1000.0f * i /
                               MOZART_RAW_SAMPLE_RATE);
    if (mozart_pre_process(ctx, input, MOZART_RAW_SAMPLES,
                           output, &meta) != 0) {
        mozart_pre_free(ctx);
        return fail("passband processing");
    }
    float tone_rms = 0.0f;
    for (int i = 32; i < MOZART_INPUT_SAMPLES; i++)
        tone_rms += output[i] * output[i];
    tone_rms = sqrtf(tone_rms / (MOZART_INPUT_SAMPLES - 32));
    if (tone_rms < 0.15f || tone_rms > 0.20f) {
        mozart_pre_free(ctx);
        return fail("1 kHz passband level changed unexpectedly");
    }
    mozart_pre_free(ctx);
#endif
    return 0;
}
