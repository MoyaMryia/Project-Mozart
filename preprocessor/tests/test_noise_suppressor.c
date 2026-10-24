// test_noise_suppressor.c — C 实现与 Python 参考实现对拍
// ============================================================================
// 夹具由 tools/gen_ns_fixture.py 生成（CMake 构建时自动运行）。
// 用法：test_noise_suppressor <fixture 目录>
#include "mozart/noise_suppressor.h"

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

static float *read_f32(const char *path, size_t *count)
{
    FILE *file = fopen(path, "rb");
    if (!file) return NULL;
    fseek(file, 0, SEEK_END);
    const long bytes = ftell(file);
    fseek(file, 0, SEEK_SET);
    float *data = malloc((size_t)bytes);
    if (data && fread(data, 1, (size_t)bytes, file) != (size_t)bytes) {
        free(data);
        data = NULL;
    }
    fclose(file);
    if (data) *count = (size_t)bytes / sizeof(float);
    return data;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "用法: %s <fixture 目录>\n", argv[0]);
        return 2;
    }
    char path[512];
    size_t in_count = 0, expected_count = 0;
    snprintf(path, sizeof(path), "%s/input.f32", argv[1]);
    float *input = read_f32(path, &in_count);
    snprintf(path, sizeof(path), "%s/expected.f32", argv[1]);
    float *expected = read_f32(path, &expected_count);
    if (!input || !expected) {
        fprintf(stderr, "夹具读取失败：%s\n", argv[1]);
        return 2;
    }

    int failures = 0;

    // ---- 对拍：确定性变长分块喂入（覆盖流式合同与数值路径） ----
    mozart_ns_t *ns = mozart_ns_new(&(mozart_ns_config_t){0});
    CHECK(ns != NULL);
    float *out = calloc(expected_count + 4096, sizeof(float));
    size_t written = 0;
    static const size_t chunks[] = {1, 7, 64, 128, 333, 960, 5, 4096};
    size_t position = 0;
    while (position < in_count) {
        const size_t take = position + chunks[position % 8] <= in_count
                                ? chunks[position % 8]
                                : in_count - position;
        written += mozart_ns_push(ns, input + position, take,
                                  out + written, expected_count + 4096 - written);
        position += take;
    }
    written += mozart_ns_flush(ns, out + written, expected_count + 4096 - written);

    CHECK(written >= expected_count - 64);
    const size_t common = written < expected_count ? written : expected_count;
    double max_diff = 0.0;
    for (size_t i = 0; i < common; i++) {
        const double diff = fabs((double)out[i] - (double)expected[i]);
        if (diff > max_diff) max_diff = diff;
    }
    printf("[ns] 对拍 %zu 样本, 最大绝对差 %.3e\n", common, max_diff);
    // 门限依据：同配置（lookahead=0）下 C 与 Python 参考实测残差 3.7e-9，
    // 门限留三个数量级余量；配置错配（如 lookahead 漂移）会显著越限。
    CHECK(max_diff < 1e-6);

    // ---- 旁路恒等：bypass 时增益恒 1，输出与输入索引对齐 ----
    mozart_ns_t *bypass = mozart_ns_new(&(mozart_ns_config_t){ .bypass = true });
    CHECK(bypass != NULL);
    const size_t probe = 48000;
    float *bout = calloc(probe + 4096, sizeof(float));
    size_t bwritten = mozart_ns_push(bypass, input, probe, bout, probe + 4096);
    bwritten += mozart_ns_flush(bypass, bout + bwritten, probe + 4096 - bwritten);
    double bmax = 0.0;
    for (size_t i = 8; i < probe && i < bwritten; i++) {
        const double diff = fabs((double)bout[i] - (double)input[i]);
        if (diff > bmax) bmax = diff;
    }
    printf("[ns] bypass 最大绝对差 %.3e（跳过前 8 样本边界）\n", bmax);
    CHECK(bmax < 1e-6);

    // ---- 置信度与遥测边界 ----
    mozart_ns_t *tiny = mozart_ns_new(&(mozart_ns_config_t){0});
    float sink[256];
    mozart_ns_push(tiny, input, 2048, sink, sizeof(sink) / sizeof(sink[0]));
    const double conf = mozart_ns_speech_confidence(tiny);
    CHECK(conf >= 0.0 && conf <= 1.0);
    CHECK(mozart_ns_consumed(tiny) >= mozart_ns_emitted(tiny));
    CHECK(mozart_ns_new(&(mozart_ns_config_t){ .frame = 999 }) == NULL);
    mozart_ns_free(tiny);

    mozart_ns_free(ns);
    mozart_ns_free(bypass);
    free(input);
    free(expected);
    free(out);
    free(bout);

    if (failures) {
        fprintf(stderr, "test_noise_suppressor: %d 项失败\n", failures);
        return 1;
    }
    printf("[OK] test_noise_suppressor\n");
    return 0;
}
