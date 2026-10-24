// test_stages.c — 噪声门 / AGC / 限幅单元测试
// ============================================================================
// 固定三条性质：关闭时逐位恒等（AI 路径等价依据）、抑制方向正确、
// 遥测字段被刷新。指标级验证见 tools/wola_reference.py 的电池。
#include "mozart/stages.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define CHECK(cond) do { \
    if (!(cond)) { \
        fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        failures++; \
    } \
} while (0)

#define RATE 48000
#define N 960

static void fill_sine(float *x, int n, float freq, float level)
{
    for (int i = 0; i < n; i++)
        x[i] = level * sinf(2.0f * (float)M_PI * freq * (float)i / (float)RATE);
}

int main(void)
{
    int failures = 0;
    static float x[N], y[N];

    // ---- 关闭 = 恒等（逐位） ----
    fill_sine(x, N, 440.0f, 0.2f);
    memcpy(y, x, sizeof(x));
    mozart_gate_t gate;
    mozart_agc_t agc;
    mozart_limiter_t limiter;
    mozart_gate_init(&gate, RATE);
    mozart_agc_init(&agc, RATE);
    mozart_limiter_init(&limiter, RATE);
    mozart_gate_process(&gate, y, N, RATE);
    mozart_agc_process(&agc, y, N, RATE);
    mozart_limiter_process(&limiter, y, N, RATE);
    CHECK(memcmp(y, x, sizeof(x)) == 0);

    // ---- 噪声门：静音关门、语音开门 ----
    mozart_gate_init(&gate, RATE);
    gate.enabled = true;
    memset(y, 0, sizeof(y));
    mozart_gate_process(&gate, y, N, RATE);
    CHECK(gate.gain < 0.01f);            // 静音关门
    fill_sine(y, N, 300.0f, 0.3f);       // 高于默认门限 -45 dB
    float ref_peak = 0.0f;
    for (int i = 0; i < N; i++) ref_peak = fabsf(y[i]) > ref_peak ? fabsf(y[i]) : ref_peak;
    for (int repeat = 0; repeat < 10; repeat++)
        mozart_gate_process(&gate, y, N, RATE);   // attack 5 ms ≈ 10 帧内开门
    CHECK(gate.gain > 0.9f);
    CHECK(gate.open_count >= 1);
    CHECK(gate.out_peak_db > -60.0f);

    // ---- AGC：低电平被抬升、不超过上限 ----
    // 每次喂新鲜副本模拟流式新样本；原地重复会让信号自我调节到目标电平。
    mozart_agc_init(&agc, RATE);
    agc.enabled = true;
    fill_sine(x, N, 500.0f, 0.01f);      // -40 dBFS
    float in_rms = 0.0f;
    for (int i = 0; i < N; i++) in_rms += x[i] * x[i];
    in_rms = sqrtf(in_rms / N);
    for (int repeat = 0; repeat < 200; repeat++) {
        memcpy(y, x, sizeof(x));
        mozart_agc_process(&agc, y, N, RATE);
    }
    float out_rms = 0.0f;
    for (int i = 0; i < N; i++) out_rms += y[i] * y[i];
    out_rms = sqrtf(out_rms / N);
    CHECK(out_rms > in_rms * 4.0f);      // 明显抬升
    CHECK(agc.gain > 0.0f && agc.gain <= agc.max_gain_db + 1e-3f);
    CHECK(agc.out_rms_db > agc.in_rms_db);

    // ---- 限幅：超 Ceiling 被压、恢复后增益回升 ----
    mozart_limiter_init(&limiter, RATE);
    limiter.enabled = true;
    fill_sine(y, N, 800.0f, 0.95f);      // 远超 -1 dB 天花板
    mozart_limiter_process(&limiter, y, N, RATE);
    float peak = 0.0f;
    for (int i = 0; i < N; i++) peak = fabsf(y[i]) > peak ? fabsf(y[i]) : peak;
    const float ceiling = powf(10.0f, limiter.ceiling_db / 20.0f);
    CHECK(peak <= ceiling * 1.01f);
    CHECK(limiter.out_peak_db <= limiter.ceiling_db + 0.1f);
    fill_sine(y, N, 800.0f, 0.1f);       // 低电平恢复
    for (int repeat = 0; repeat < 100; repeat++)
        mozart_limiter_process(&limiter, y, N, RATE);
    CHECK(limiter.gain > 0.9f);

    if (failures) {
        fprintf(stderr, "test_stages: %d 项失败\n", failures);
        return 1;
    }
    printf("[OK] test_stages\n");
    return 0;
}
