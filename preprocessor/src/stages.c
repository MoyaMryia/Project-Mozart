// stages.c — 噪声门 / AGC / 峰值限幅实现
// ============================================================================
// 约定：enabled=false 时 process 立即返回且不触碰样本与遥测——
// 这是"新增舞台默认关闭 = AI 路径逐字节等价"的依据，测试固定该性质。
#include "mozart/stages.h"

#include <math.h>
#include <string.h>

#define MOZART_SILENCE_DB (-120.0f)

float mozart_peak_db(const float *x, size_t n)
{
    float peak = 0.0f;
    for (size_t i = 0; i < n; i++) {
        const float a = fabsf(x[i]);
        if (a > peak) peak = a;
    }
    return peak > 0.0f ? 20.0f * log10f(peak) : MOZART_SILENCE_DB;
}

float mozart_rms_db(const float *x, size_t n)
{
    float power = 0.0f;
    for (size_t i = 0; i < n; i++) power += x[i] * x[i];
    const float rms = sqrtf(power / (float)n);
    return rms > 0.0f ? 20.0f * log10f(rms) : MOZART_SILENCE_DB;
}

void mozart_gate_init(mozart_gate_t *g, unsigned rate)
{
    memset(g, 0, sizeof(*g));
    g->threshold_db = -45.0f;
    g->attack_ms  = 5.0f;
    g->hold_ms    = 60.0f;
    g->release_ms = 150.0f;
    g->gain = 0.0f;
    g->in_peak_db = g->out_peak_db = MOZART_SILENCE_DB;
    (void)rate;
}

void mozart_gate_process(mozart_gate_t *g, float *x, size_t n, unsigned rate)
{
    if (!g->enabled || n == 0) return;

    const float rms = mozart_rms_db(x, n);
    g->in_peak_db = mozart_peak_db(x, n);

    // 开关门按块判定；保持时长折算为块数（向上取整，至少 1 块）。
    const int hold_blocks =
        (int)((g->hold_ms / 1000.0f) * (float)rate / (float)n) + 1;
    if (rms >= g->threshold_db) {
        if (!g->open) { g->open = true; g->open_count++; }
        g->hold_blocks = hold_blocks;
    } else if (g->open && --g->hold_blocks < 0) {
        g->open = false;
        g->close_count++;
    }

    const float target = g->open ? 1.0f : 0.0f;
    const float span_s = (g->open ? g->attack_ms : g->release_ms) / 1000.0f;
    const float step = span_s > 0.0f
        ? 1.0f / (span_s * (float)rate) : 1.0f;
    for (size_t i = 0; i < n; i++) {
        if (g->gain < target) {
            g->gain += step;
            if (g->gain > target) g->gain = target;
        } else if (g->gain > target) {
            g->gain -= step;
            if (g->gain < target) g->gain = target;
        }
        x[i] *= g->gain;
    }
    g->out_peak_db = mozart_peak_db(x, n);
}

void mozart_agc_init(mozart_agc_t *a, unsigned rate)
{
    memset(a, 0, sizeof(*a));
    a->target_db   = -20.0f;
    a->max_gain_db = 24.0f;
    // 每块（≈20 ms）一阶平滑：上升 ~1 s 收敛，下降 ~100 ms 收敛。
    a->up_coeff   = 0.02f;
    a->down_coeff = 0.20f;
    a->in_rms_db = a->out_rms_db = MOZART_SILENCE_DB;
    (void)rate;
}

void mozart_agc_process(mozart_agc_t *a, float *x, size_t n, unsigned rate)
{
    if (!a->enabled || n == 0) return;

    const float rms = mozart_rms_db(x, n);
    a->in_rms_db = rms;

    float desired = a->target_db - rms;
    if (desired > a->max_gain_db) desired = a->max_gain_db;
    if (desired < 0.0f) desired = 0.0f;

    const float coeff = desired > a->gain ? a->up_coeff : a->down_coeff;
    a->gain += coeff * (desired - a->gain);

    const float gain = powf(10.0f, a->gain / 20.0f);
    for (size_t i = 0; i < n; i++) x[i] *= gain;

    a->out_rms_db = rms + a->gain;
    (void)rate;
}

void mozart_limiter_init(mozart_limiter_t *l, unsigned rate)
{
    memset(l, 0, sizeof(*l));
    l->ceiling_db = -1.0f;
    l->release_ms = 80.0f;
    l->env  = 0.0f;
    l->gain = 1.0f;
    l->in_peak_db = l->out_peak_db = MOZART_SILENCE_DB;
    (void)rate;
}

void mozart_limiter_process(mozart_limiter_t *l, float *x, size_t n, unsigned rate)
{
    if (!l->enabled || n == 0) return;

    const float ceiling = powf(10.0f, l->ceiling_db / 20.0f);
    const float rel_s = l->release_ms / 1000.0f;
    // 包络指数衰减系数与增益恢复系数：e^{-1/(t·rate)}
    const float env_coef   = expf(-1.0f / (rel_s * (float)rate));
    const float recover    = 1.0f - expf(-1.0f / (rel_s * (float)rate));

    l->in_peak_db = mozart_peak_db(x, n);
    for (size_t i = 0; i < n; i++) {
        const float a = fabsf(x[i]);
        // 立即 attack：包络取当前峰值；否则按 release 指数回落。
        l->env = a > l->env * env_coef ? a : l->env * env_coef;
        if (l->env < ceiling) {
            l->gain += (1.0f - l->gain) * recover;
        } else {
            const float need = ceiling / l->env;
            if (need < l->gain) l->gain = need;
        }
        x[i] *= l->gain;
    }
    l->out_peak_db = mozart_peak_db(x, n);
}
