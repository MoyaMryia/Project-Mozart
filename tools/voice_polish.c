// 本机离线人声音色美化算子；参数为固定档位常量，由测试约束范围。
#include "mozart/stages.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#define POLISH_PI 3.14159265358979323846
#define POLISH_ROOM_TAPS 2
#define POLISH_BUTTERWORTH_Q 0.7071067811865476

typedef struct {
    double b0, b1, b2, a1, a2;
    double z1, z2;
} polish_biquad_t;

typedef struct {
    float *buffer;
    size_t length;
    size_t position;
    double lowpass;
} polish_tap_t;

struct polish_config {
    double highpass_hz;
    double low_peak_hz, low_peak_db, low_peak_q;
    double mid_peak_hz, mid_peak_db, mid_peak_q;
    double presence_hz, presence_db, presence_q;
    double air_hz, air_db;
    double room_ms, room_mix;
    double gated_presence_db, gated_air_db, gate_ratio;
};

typedef struct {
    unsigned rate;
    int bypass;
    int highpass, low_peak, mid_peak, presence, air;
    polish_biquad_t hp, low, mid, pres, shelf;
    polish_tap_t taps[POLISH_ROOM_TAPS];
    double room_mix, room_lowpass_a;
    int room;
    int gated;
    polish_biquad_t gate_low, gate_presence, gate_air;
    double presence_step, air_step, gate_ratio;
    double gate_fast, gate_slow, gate_weight;
    double gate_attack, gate_release, gate_slow_coeff;
    unsigned gate_init, gate_init_samples;
    mozart_limiter_t limiter;
} polish_t;

static void peaking_init(polish_biquad_t *b, unsigned rate, double hz, double gain_db, double q)
{
    const double A = pow(10.0, gain_db / 40.0);
    const double w = 2.0 * POLISH_PI * hz / rate;
    const double c = cos(w), s = sin(w), alpha = s / (2.0 * q);
    const double a0 = 1.0 + alpha / A;
    b->b0 = (1.0 + alpha * A) / a0;
    b->b1 = (-2.0 * c) / a0;
    b->b2 = (1.0 - alpha * A) / a0;
    b->a1 = (-2.0 * c) / a0;
    b->a2 = (1.0 - alpha / A) / a0;
    b->z1 = b->z2 = 0.0;
}

static void highpass_init(polish_biquad_t *b, unsigned rate, double hz, double q)
{
    const double w = 2.0 * POLISH_PI * hz / rate;
    const double c = cos(w), alpha = sin(w) / (2.0 * q);
    const double a0 = 1.0 + alpha;
    b->b0 = (1.0 + c) * 0.5 / a0;
    b->b1 = -(1.0 + c) / a0;
    b->b2 = (1.0 + c) * 0.5 / a0;
    b->a1 = (-2.0 * c) / a0;
    b->a2 = (1.0 - alpha) / a0;
    b->z1 = b->z2 = 0.0;
}

static void high_shelf_init(polish_biquad_t *b, unsigned rate, double hz, double gain_db)
{
    const double A = pow(10.0, gain_db / 40.0);
    const double w = 2.0 * POLISH_PI * hz / rate;
    const double c = cos(w), s = sin(w);
    const double beta = 2.0 * sqrt(A) * s * POLISH_BUTTERWORTH_Q;
    const double a0 = (A + 1.0) - (A - 1.0) * c + beta;
    b->b0 = A * ((A + 1.0) + (A - 1.0) * c + beta) / a0;
    b->b1 = -2.0 * A * ((A - 1.0) + (A + 1.0) * c) / a0;
    b->b2 = A * ((A + 1.0) + (A - 1.0) * c - beta) / a0;
    b->a1 = 2.0 * ((A - 1.0) - (A + 1.0) * c) / a0;
    b->a2 = ((A + 1.0) - (A - 1.0) * c - beta) / a0;
    b->z1 = b->z2 = 0.0;
}

static float polish_biquad(polish_biquad_t *b, float input)
{
    const double x = input;
    const double y = b->b0 * x + b->z1;
    b->z1 = b->b1 * x - b->a1 * y + b->z2;
    b->z2 = b->b2 * x - b->a2 * y;
    return (float)y;
}

static void lowpass_init(polish_biquad_t *b, unsigned rate, double hz, double q)
{
    const double w = 2.0 * POLISH_PI * hz / rate;
    const double c = cos(w), alpha = sin(w) / (2.0 * q);
    const double a0 = 1.0 + alpha;
    b->b0 = (1.0 - c) * 0.5 / a0;
    b->b1 = (1.0 - c) / a0;
    b->b2 = (1.0 - c) * 0.5 / a0;
    b->a1 = (-2.0 * c) / a0;
    b->a2 = (1.0 - alpha) / a0;
    b->z1 = b->z2 = 0.0;
}

static void bandpass_init(polish_biquad_t *b, unsigned rate, double hz, double q)
{
    const double w = 2.0 * POLISH_PI * hz / rate;
    const double c = cos(w), alpha = sin(w) / (2.0 * q);
    const double a0 = 1.0 + alpha;
    b->b0 = alpha / a0;
    b->b1 = 0.0;
    b->b2 = -alpha / a0;
    b->a1 = (-2.0 * c) / a0;
    b->a2 = (1.0 - alpha) / a0;
    b->z1 = b->z2 = 0.0;
}

static void polish_limiter_setup(polish_t *p)
{
    mozart_limiter_init(&p->limiter, p->rate);
    p->limiter.enabled = !p->bypass;
}

void *polish_new(unsigned rate, const struct polish_config *config)
{
    polish_t *p = calloc(1, sizeof(*p));
    if (!p) return NULL;
    p->rate = rate;
    const size_t room_base = (size_t)(config->room_ms * rate / 1000.0);
    p->highpass = config->highpass_hz > 0.0;
    p->low_peak = config->low_peak_db != 0.0;
    p->mid_peak = config->mid_peak_db != 0.0;
    p->presence = config->presence_db != 0.0;
    p->air = config->air_db != 0.0;
    p->room = config->room_mix > 0.0 && room_base > 0;
    p->gated = config->gated_presence_db != 0.0 || config->gated_air_db != 0.0;
    p->bypass = !(p->highpass || p->low_peak || p->mid_peak || p->presence || p->air ||
                  p->room || p->gated);

    if (p->highpass) highpass_init(&p->hp, rate, config->highpass_hz, POLISH_BUTTERWORTH_Q);
    if (p->low_peak)
        peaking_init(&p->low, rate, config->low_peak_hz, config->low_peak_db, config->low_peak_q);
    if (p->mid_peak)
        peaking_init(&p->mid, rate, config->mid_peak_hz, config->mid_peak_db, config->mid_peak_q);
    if (p->presence)
        peaking_init(&p->pres, rate, config->presence_hz, config->presence_db, config->presence_q);
    if (p->air) high_shelf_init(&p->shelf, rate, config->air_hz, config->air_db);

    if (p->gated) {
        p->presence_step = pow(10.0, config->gated_presence_db / 20.0) - 1.0;
        p->air_step = pow(10.0, config->gated_air_db / 20.0) - 1.0;
        p->gate_ratio = config->gate_ratio;
        lowpass_init(&p->gate_low, rate, 500.0, POLISH_BUTTERWORTH_Q);
        bandpass_init(&p->gate_presence, rate, config->presence_hz, config->presence_q);
        highpass_init(&p->gate_air, rate, config->air_hz, POLISH_BUTTERWORTH_Q);
        p->gate_attack = 1.0 - exp(-1.0 / (0.020 * rate));
        p->gate_release = 1.0 - exp(-1.0 / (0.100 * rate));
        p->gate_slow_coeff = 1.0 - exp(-1.0 / (0.300 * rate));
        p->gate_init_samples = rate / 10;
    }

    if (p->room) {
        p->room_mix = config->room_mix;
        p->room_lowpass_a = 1.0 - exp(-2.0 * POLISH_PI * 5000.0 / rate);
        for (int i = 0; i < POLISH_ROOM_TAPS; ++i) {
            p->taps[i].length = room_base + (size_t)(i * room_base / 2);
            p->taps[i].buffer = calloc(p->taps[i].length, sizeof(float));
            if (!p->taps[i].buffer) {
                for (int j = 0; j < i; ++j) free(p->taps[j].buffer);
                free(p);
                return NULL;
            }
        }
    }

    polish_limiter_setup(p);
    return p;
}

static float polish_room(polish_t *p, float input)
{
    double wet = 0.0;
    for (int i = 0; i < POLISH_ROOM_TAPS; ++i) {
        polish_tap_t *t = &p->taps[i];
        const double delayed = t->buffer[t->position];
        t->buffer[t->position] = input;
        t->position = (t->position + 1) % t->length;
        t->lowpass += p->room_lowpass_a * (delayed - t->lowpass);
        wet += t->lowpass;
    }
    wet /= POLISH_ROOM_TAPS;
    return (float)(input * (1.0 - p->room_mix) + wet * p->room_mix);
}

static float polish_gated(polish_t *p, float input)
{
    const float low = polish_biquad(&p->gate_low, input);
    const double energy = fabs(low);
    p->gate_fast += p->gate_attack * (energy - p->gate_fast);
    if (p->gate_init < p->gate_init_samples) {
        p->gate_init++;
        p->gate_slow += (energy - p->gate_slow) / (double)p->gate_init;
    } else {
        p->gate_slow += p->gate_slow_coeff * (energy - p->gate_slow);
    }
    const double ratio = p->gate_fast / (p->gate_slow + 1e-9);
    double target = (ratio - 1.0) / (p->gate_ratio - 1.0);
    target = target < 0.0 ? 0.0 : (target > 1.0 ? 1.0 : target);
    const double coeff = target > p->gate_weight ? p->gate_attack : p->gate_release;
    p->gate_weight += coeff * (target - p->gate_weight);
    const float presence = polish_biquad(&p->gate_presence, input);
    const float air = polish_biquad(&p->gate_air, input);
    return (float)(input + p->presence_step * p->gate_weight * presence +
                   p->air_step * p->gate_weight * air);
}

void polish_process(void *state, const float *input, float *output, size_t n)
{
    polish_t *p = state;
    if (p->bypass) {
        if (input != output) memmove(output, input, n * sizeof(float));
        return;
    }
    for (size_t i = 0; i < n; ++i) {
        float x = input[i];
        if (p->highpass) x = polish_biquad(&p->hp, x);
        if (p->low_peak) x = polish_biquad(&p->low, x);
        if (p->mid_peak) x = polish_biquad(&p->mid, x);
        if (p->presence) x = polish_biquad(&p->pres, x);
        if (p->air) x = polish_biquad(&p->shelf, x);
        if (p->gated) x = polish_gated(p, x);
        if (p->room) x = polish_room(p, x);
        output[i] = x;
    }
    mozart_limiter_process(&p->limiter, output, n, p->rate);
}

void polish_reset(void *state)
{
    polish_t *p = state;
    polish_biquad_t *filters[] = {&p->hp, &p->low, &p->mid, &p->pres, &p->shelf};
    for (size_t i = 0; i < sizeof(filters) / sizeof(filters[0]); ++i)
        filters[i]->z1 = filters[i]->z2 = 0.0;
    polish_biquad_t *gates[] = {&p->gate_low, &p->gate_presence, &p->gate_air};
    for (size_t i = 0; i < sizeof(gates) / sizeof(gates[0]); ++i)
        gates[i]->z1 = gates[i]->z2 = 0.0;
    p->gate_fast = p->gate_slow = p->gate_weight = 0.0;
    p->gate_init = 0;
    for (int i = 0; i < POLISH_ROOM_TAPS; ++i) {
        if (p->taps[i].buffer)
            memset(p->taps[i].buffer, 0, p->taps[i].length * sizeof(float));
        p->taps[i].position = 0;
        p->taps[i].lowpass = 0.0;
    }
    polish_limiter_setup(p);
}

void polish_free(void *state)
{
    polish_t *p = state;
    if (!p) return;
    for (int i = 0; i < POLISH_ROOM_TAPS; ++i) free(p->taps[i].buffer);
    free(p);
}

size_t polish_config_size(void)
{
    return sizeof(struct polish_config);
}

double polish_highpass_q(void)
{
    return POLISH_BUTTERWORTH_Q;
}

size_t polish_first_echo_samples(unsigned rate, double room_ms)
{
    return (size_t)(room_ms * rate / 1000.0);
}

double polish_limiter_ceiling(unsigned rate)
{
    mozart_limiter_t limiter;
    mozart_limiter_init(&limiter, rate);
    return pow(10.0, limiter.ceiling_db / 20.0);
}
