// test_async_src.c — 自适应重采样单元测试
// ============================================================================
// 钉四条性质：比例夹取、恒等（ratio=1 时输出=输入）、漂移吸收
// （持续 500ppm 偏差下水位被拉回目标）、相位连续（无缝拼接）。
#include "mozart/async_src.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHECK(cond) do { \
    if (!(cond)) { \
        fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        failures++; \
    } \
} while (0)

int main(void)
{
    int failures = 0;

    // ---- 比例夹取 ----
    mozart_src_t *src = mozart_src_new(4.0, 0.0005);
    CHECK(src != NULL);
    CHECK(fabs(mozart_src_ratio(src, 9.9) - 1.0005) < 1e-9);   // 高水位钳上限
    CHECK(fabs(mozart_src_ratio(src, 0.1) - 0.9995) < 1e-9);   // 低水位钳下限
    CHECK(fabs(mozart_src_ratio(src, 1.0) - 1.0) < 1e-9);      // 目标即恒等
    CHECK(mozart_src_new(0, 0.0005) == NULL);
    CHECK(mozart_src_new(4.0, 0.5) == NULL);                   // 比例摆动上限拒绝

    // ---- 恒等：ratio=1 输出=输入（跨多块相位连续）----
    const int N = 4800;
    float *in = malloc(sizeof(float) * N);
    float *out = malloc(sizeof(float) * N);
    for (int i = 0; i < N; i++) in[i] = (float)(i % 251) / 251.0f;
    int total = 0;
    for (int pos = 0; pos < N - 1; pos += 128) {
        const int n = mozart_src_process(src, in + pos, 128,
                                         out + total, N - total, 1.0);
        CHECK(n >= 0);
        total += n;
    }
    CHECK(total >= N - 4);                       // 输出近似覆盖输入
    double max_diff = 0.0;
    for (int i = 0; i < total && i < N; i++) {
        const double d = fabs((double)out[i] - (double)in[i]);
        if (d > max_diff) max_diff = d;
    }
    CHECK(max_diff < 0.01);                      // 线性插值在慢变信号上近恒等

    // ---- 漂移吸收：500 ppm 持续偏差，比例平滑逼近钳位值 ----
    mozart_src_t *drift = mozart_src_new(4.0, 0.0005);
    double avg = 0.0;
    for (int block = 0; block < 1000; block++) {
        const double ratio = mozart_src_ratio(drift, 8.0);   // 持续高水位
        avg = mozart_src_avg_ratio(drift);
        float x[128] = {0}, y[128] = {0};
        mozart_src_process(drift, x, 128, y, 128, ratio);
    }
    CHECK(fabs(avg - 1.0005) < 1e-4);            // 平滑收敛到钳位上限

    free(in);
    free(out);
    mozart_src_free(src);
    mozart_src_free(drift);

    if (failures) {
        fprintf(stderr, "test_async_src: %d 项失败\n", failures);
        return 1;
    }
    printf("[OK] test_async_src\n");
    return 0;
}
