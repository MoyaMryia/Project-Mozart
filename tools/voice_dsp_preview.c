// 本机离线试听均衡算子；Python 入口负责参数校验。
#include "mozart/stages.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    double b0, b1, b2, a1, a2;
    double z1, z2;
} biquad_t;

typedef struct {
    unsigned rate;
    int eq_low, eq_presence;
    biquad_t low, presence;
    mozart_limiter_t limiter;
} voice_eq_t;

static void peaking_init(biquad_t *b, unsigned rate, double hz, double gain_db, double q)
{
    const double A = pow(10.0, gain_db / 40.0);
    const double w = 2.0 * 3.14159265358979323846 * hz / rate;
    const double c = cos(w), s = sin(w), alpha = s / (2.0 * q);
    const double a0 = 1.0 + alpha / A;
    b->b0 = (1.0 + alpha * A) / a0;
    b->b1 = (-2.0 * c) / a0;
    b->b2 = (1.0 - alpha * A) / a0;
    b->a1 = (-2.0 * c) / a0;
    b->a2 = (1.0 - alpha / A) / a0;
    b->z1 = b->z2 = 0.0;
}

static float biquad_process(biquad_t *b, float input)
{
    const double x = input;
    const double y = b->b0 * x + b->z1;
    b->z1 = b->b1 * x - b->a1 * y + b->z2;
    b->z2 = b->b2 * x - b->a2 * y;
    return (float)y;
}

void *voice_eq_new(unsigned rate, double low_hz, double low_db,
                   double presence_hz, double presence_db, double q)
{
    voice_eq_t *v = calloc(1, sizeof(*v));
    if (!v) return NULL;
    v->rate = rate;
    v->eq_low = low_db != 0.0;
    v->eq_presence = presence_db != 0.0;
    if (v->eq_low) peaking_init(&v->low, rate, low_hz, low_db, q);
    if (v->eq_presence) peaking_init(&v->presence, rate, presence_hz, presence_db, q);
    mozart_limiter_init(&v->limiter, rate);
    v->limiter.enabled = v->eq_low || v->eq_presence;
    return v;
}

void voice_eq_process(void *state, const float *input, float *output, size_t n)
{
    voice_eq_t *v = state;
    if (!v->eq_low && !v->eq_presence) {
        if (input != output) memmove(output, input, n * sizeof(float));
        return;
    }
    for (size_t i = 0; i < n; ++i) {
        float x = input[i];
        if (v->eq_low) x = biquad_process(&v->low, x);
        if (v->eq_presence) x = biquad_process(&v->presence, x);
        output[i] = x;
    }
    mozart_limiter_process(&v->limiter, output, n, v->rate);
}

void voice_eq_reset(void *state)
{
    voice_eq_t *v = state;
    v->low.z1 = v->low.z2 = v->presence.z1 = v->presence.z2 = 0.0;
    mozart_limiter_init(&v->limiter, v->rate);
    v->limiter.enabled = v->eq_low || v->eq_presence;
}

void voice_eq_free(void *state)
{
    free(state);
}
