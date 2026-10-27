// 离线清理原型；调用方提供已校验的配置。
#include <math.h>
#include <stdlib.h>
#include <string.h>

#define CLEANUP_ACTIVE_GAIN 0.99

struct cleanup_config {
    double notch_1_hz, notch_2_hz, notch_3_hz, notch_q;
    double lowpass_hz;
    double gate_open_db, gate_close_db, gate_floor_db;
    double gate_hold_ms, gate_attack_ms, gate_release_ms;
    double plosive_max_db, plosive_ratio;
};

typedef struct { double b0, b1, b2, a1, a2, z1, z2; } biquad;
typedef struct {
    biquad notch[3], lp[2];
    int notch_on[3], lp_on, gate_on, plosive_on, bypass;
    double gate_open, gate_close, gate_floor, gate_env, gate_gain;
    double gate_attack, gate_release;
    size_t gate_hold, hold_left;
    double low_alpha, low_slow_alpha, low_fast, low_slow, mid_env, split_alpha, split_z;
    double plosive_ratio, plosive_target, plosive_gain, plosive_attack, plosive_release;
    int gate_opened;
    double samples, gate_count, plosive_count;
    double min_gate, min_plosive, max_abs;
} cleanup_state;

static void set_notch(biquad *b, double rate, double hz, double q) {
    const double w = 2.0 * 3.14159265358979323846 * hz / rate;
    const double c = cos(w), a = sin(w) / (2.0 * q);
    const double norm = 1.0 / (1.0 + a);
    b->b0 = norm; b->b1 = -2.0 * c * norm; b->b2 = norm;
    b->a1 = -2.0 * c * norm; b->a2 = (1.0 - a) * norm;
}
static void set_lowpass(biquad *b, double rate, double hz, double q) {
    const double w = 2.0 * 3.14159265358979323846 * hz / rate;
    const double c = cos(w), a = sin(w) / (2.0 * q);
    const double norm = 1.0 / (1.0 + a);
    b->b0 = ((1.0 - c) * 0.5) * norm; b->b1 = (1.0 - c) * norm;
    b->b2 = b->b0; b->a1 = -2.0 * c * norm; b->a2 = (1.0 - a) * norm;
}
static double run_biquad(biquad *b, double x) {
    const double y = b->b0 * x + b->z1;
    b->z1 = b->b1 * x - b->a1 * y + b->z2;
    b->z2 = b->b2 * x - b->a2 * y;
    return y;
}
static double coeff(double ms, unsigned rate) {
    return exp(-1.0 / (0.001 * ms * (double)rate));
}

void *cleanup_new(unsigned rate, const struct cleanup_config *cfg) {
    cleanup_state *s = (cleanup_state *)calloc(1, sizeof(*s));
    if (!s) return NULL;
    const double nhz[3] = {cfg->notch_1_hz, cfg->notch_2_hz, cfg->notch_3_hz};
    for (int i = 0; i < 3; ++i) {
        if (nhz[i] != 0.0) { set_notch(&s->notch[i], rate, nhz[i], cfg->notch_q); s->notch_on[i] = 1; }
    }
    if (cfg->lowpass_hz != 0.0) {
        set_lowpass(&s->lp[0], rate, cfg->lowpass_hz, 0.541196100146197);
        set_lowpass(&s->lp[1], rate, cfg->lowpass_hz, 1.306562964876377); s->lp_on = 1;
    }
    s->gate_on = cfg->gate_floor_db != 0.0;
    s->gate_open = pow(10.0, cfg->gate_open_db / 10.0);
    s->gate_close = pow(10.0, cfg->gate_close_db / 10.0);
    s->gate_floor = pow(10.0, cfg->gate_floor_db / 20.0);
    s->gate_attack = coeff(cfg->gate_attack_ms, rate); s->gate_release = coeff(cfg->gate_release_ms, rate);
    s->gate_hold = (size_t)(cfg->gate_hold_ms * 0.001 * rate + 0.5);
    s->gate_gain = 1.0; s->min_gate = 1.0; s->min_plosive = 1.0;
    s->low_alpha = coeff(5.0, rate); s->low_slow_alpha = coeff(120.0, rate);
    s->split_alpha = exp(-2.0 * 3.14159265358979323846 * 180.0 / rate); s->low_fast = s->low_slow = 0.0;
    s->mid_env = 0.0;
    s->plosive_on = cfg->plosive_max_db != 0.0;
    s->plosive_ratio = cfg->plosive_ratio;
    s->plosive_target = pow(10.0, cfg->plosive_max_db / 20.0);
    s->plosive_gain = 1.0;
    s->plosive_attack = coeff(2.0, rate); s->plosive_release = coeff(80.0, rate);
    s->bypass = !s->lp_on && !s->gate_on && !s->plosive_on &&
                !s->notch_on[0] && !s->notch_on[1] && !s->notch_on[2];
    return s;
}

void cleanup_process(void *handle, const float *input, float *output, size_t count) {
    cleanup_state *s = (cleanup_state *)handle;
    if (s->bypass) {
        if (input != output) memmove(output, input, count * sizeof(float));
        for (size_t i = 0; i < count; ++i) { double a = fabs(output[i]); if (a > s->max_abs) s->max_abs = a; }
        s->samples += count; return;
    }
    for (size_t i = 0; i < count; ++i) {
        double x = input[i];
        for (int j = 0; j < 3; ++j) if (s->notch_on[j]) x = run_biquad(&s->notch[j], x);
        if (s->lp_on) { x = run_biquad(&s->lp[0], x); x = run_biquad(&s->lp[1], x); }
        double y = x;
        if (s->plosive_on) {
            // 一阶低频分支，快慢能量包络识别短时低频突增。
            double low = (1.0 - s->split_alpha) * x + s->split_alpha * s->split_z;
            s->split_z = low;
            double mid = x - low;
            double le = low * low, me = mid * mid;
            s->low_fast = s->low_alpha * s->low_fast + (1.0 - s->low_alpha) * le;
            s->low_slow = s->low_slow_alpha * s->low_slow + (1.0 - s->low_slow_alpha) * le;
            s->mid_env = s->low_alpha * s->mid_env + (1.0 - s->low_alpha) * me;
            int hit = s->low_fast > s->plosive_ratio * s->mid_env &&
                      s->low_fast > s->plosive_ratio * s->low_slow;
            double target = hit ? s->plosive_target : 1.0;
            double c = target < s->plosive_gain ? s->plosive_attack : s->plosive_release;
            s->plosive_gain = c * s->plosive_gain + (1.0 - c) * target;
            if (s->plosive_gain < CLEANUP_ACTIVE_GAIN) s->plosive_count += 1.0;
            if (s->plosive_gain < s->min_plosive) s->min_plosive = s->plosive_gain;
            y = low * s->plosive_gain + mid;
        }
        if (s->gate_on) {
            double power = x * x;
            s->gate_env = s->gate_attack * s->gate_env + (1.0 - s->gate_attack) * power;
            if (s->gate_env >= s->gate_open) { s->gate_opened = 1; s->hold_left = s->gate_hold; }
            else {
                if (s->hold_left) --s->hold_left;
                if (s->gate_env < s->gate_close && s->hold_left == 0) s->gate_opened = 0;
            }
            double target = s->gate_opened ? 1.0 : s->gate_floor;
            double c = target > s->gate_gain ? s->gate_attack : s->gate_release;
            s->gate_gain = c * s->gate_gain + (1.0 - c) * target;
            if (s->gate_gain < CLEANUP_ACTIVE_GAIN) s->gate_count += 1.0;
            if (s->gate_gain < s->min_gate) s->min_gate = s->gate_gain;
            y *= s->gate_gain;
        }
        output[i] = (float)y;
        double a = fabs(y); if (a > s->max_abs) s->max_abs = a;
        s->samples += 1.0;
    }
}
void cleanup_reset(void *handle) {
    cleanup_state *s = (cleanup_state *)handle;
    // 保留系数和配置，清空滤波器及包络状态。
    for (int i = 0; i < 3; ++i) s->notch[i].z1 = s->notch[i].z2 = 0.0;
    for (int i = 0; i < 2; ++i) s->lp[i].z1 = s->lp[i].z2 = 0.0;
    s->gate_env = 0.0; s->gate_gain = 1.0; s->hold_left = 0; s->gate_opened = 0;
    s->low_fast = s->low_slow = s->mid_env = s->split_z = 0.0;
    s->plosive_gain = 1.0; s->samples = s->gate_count = s->plosive_count = s->max_abs = 0.0;
    s->min_gate = s->min_plosive = 1.0;
}
void cleanup_free(void *handle) { free(handle); }
void cleanup_get_stats(void *handle, double *out) {
    cleanup_state *s = (cleanup_state *)handle;
    out[0] = s->samples; out[1] = s->gate_count; out[2] = s->plosive_count;
    out[3] = s->min_gate; out[4] = s->min_plosive; out[5] = s->max_abs;
}
