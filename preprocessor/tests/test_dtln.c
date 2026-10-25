// test_dtln.c — DTLN C 实现与 Python 参考实现对拍
// golden 夹具由 rvc-golden/dtln/validate_dtln.py 的链生成（见 fixture/meta.json）。
// 用法：test_dtln <model1> <model2> <input.f32> <golden.f32>
#include "mozart/dtln.h"
#include <stdio.h>
#include <stdlib.h>
#include <math.h>

static float *read_f32(const char *path, int *count) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END); long bytes = ftell(f); fseek(f, 0, SEEK_SET);
    float *data = malloc(bytes);
    if (data && fread(data, 1, bytes, f) != (size_t)bytes) {
        free(data);
        data = NULL;
    }
    fclose(f);
    if (data) *count = bytes / 4;
    return data;
}

int main(int argc, char **argv) {
    if (argc != 5) { fprintf(stderr, "用法: %s model1 model2 input golden\n", argv[0]); return 2; }
    int in_n = 0, gold_n = 0;
    float *input = read_f32(argv[3], &in_n);
    float *golden = read_f32(argv[4], &gold_n);
    if (!input || !golden) {
        fprintf(stderr, "夹具读取失败\n");
        return 2;
    }
    mozart_dtln_t *dt = mozart_dtln_new(argv[1], argv[2], 1);
    if (!dt) return 1;
    float *out = malloc(sizeof(float) * (in_n + 512));
    int written = 0;
    for (int pos = 0; pos + 128 <= in_n; pos += 128) {
        const int n = mozart_dtln_push(dt, input + pos, 128,
                                       out + written, in_n + 512 - written);
        if (n < 0) { fprintf(stderr, "push 失败\n"); return 1; }
        written += n;
    }
    printf("written=%d golden=%d us/block=%.0f\n", written, gold_n,
           mozart_dtln_us_per_block(dt));
    // golden 取自 Python 全流 out[384:]：流式延迟 = block_len - block_shift = 384
    const int latency = 384;
    if (written < latency + gold_n) { fprintf(stderr, "输出不足\n"); return 1; }
    double max_diff = 0.0;
    for (int i = 0; i < gold_n; i++) {
        const double diff = fabs((double)out[latency + i] - (double)golden[i]);
        if (diff > max_diff) max_diff = diff;
    }
    printf("max_diff=%.3e\n", max_diff);
    const int failures = max_diff < 1e-5 ? 0 : 1;
    free(input); free(golden); free(out);
    mozart_dtln_free(dt);
    if (failures) {
        fprintf(stderr, "对拍超限\n");
        return 1;
    }
    printf("[OK] test_dtln\n");
    return 0;
}
