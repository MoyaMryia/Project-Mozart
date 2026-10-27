// test_noise_suppressor.c — C 实现与 Python 参考实现对拍
// ============================================================================
// 夹具由 tools/gen_ns_fixture.py 生成（CMake 构建时自动运行）。
// 用法：test_noise_suppressor <fixture 目录>
#include "mozart/noise_suppressor.h"
#include "gates.h"

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

static int failures;

static float *read_f32(const char *path, size_t *count)
{
    FILE *file = fopen(path, "rb");
    if (!file) return NULL;
    if (fseek(file, 0, SEEK_END) != 0) { fclose(file); return NULL; }
    const long bytes = ftell(file);
    if (bytes < 0 || bytes % (long)sizeof(float) != 0
        || fseek(file, 0, SEEK_SET) != 0) {
        fclose(file);
        return NULL;
    }
    float *data = malloc((size_t)bytes);
    if (bytes != 0 && !data) { fclose(file); return NULL; }
    if (bytes != 0 && fread(data, 1, (size_t)bytes, file) != (size_t)bytes) {
        free(data);
        fclose(file);
        return NULL;
    }
    fclose(file);
    *count = (size_t)bytes / sizeof(float);
    return data;
}

static float *process(const float *input, size_t count, size_t *written,
                      mozart_ns_t *ns)
{
    const size_t capacity = count + 4096;
    float *out = calloc(capacity, sizeof(float));
    if (!out) return NULL;
    *written = 0;
    static const size_t chunks[] = {1, 7, 64, 128, 333, 960, 5, 4096};
    size_t position = 0;
    while (position < count) {
        const size_t chunk = chunks[position % 8];
        const size_t take = chunk < count - position ? chunk : count - position;
        *written += mozart_ns_push(ns, input + position, take,
                                   out + *written, capacity - *written);
        position += take;
    }
    *written += mozart_ns_flush(ns, out + *written, capacity - *written);
    return out;
}

static float next_white(uint32_t *state)
{
    *state ^= *state << 13;
    *state ^= *state >> 17;
    *state ^= *state << 5;
    return ((float)(*state >> 8) / 16777216.0f * 2.0f - 1.0f) * 0.05f;
}

static void test_white_noise_prefixes(void)
{
    const struct { size_t prefix, initial_noise; } cases[] = {
        {0, 0}, {4800, 0}, {48000, 0}, {9 * 48000, 48000}
    };
    for (size_t test = 0; test < sizeof(cases) / sizeof(cases[0]); test++) {
        const size_t prefix = cases[test].prefix;
        const size_t noise_count = 3 * 48000;
        const size_t count = prefix + noise_count;
        float *input = calloc(count, sizeof(float));
        if (!input) { fprintf(stderr, "白噪声测试内存分配失败\n"); exit(2); }
        uint32_t state = 0x13579bdu;
        for (size_t i = 0; i < cases[test].initial_noise; i++) input[i] = next_white(&state);
        for (size_t i = prefix; i < count; i++) input[i] = next_white(&state);
        mozart_ns_t *ns = mozart_ns_new(&(mozart_ns_config_t){0});
        if (!ns) { free(input); fprintf(stderr, "降噪器创建失败\n"); exit(2); }
        size_t written = 0;
        float *out = process(input, count, &written, ns);
        if (!out) { mozart_ns_free(ns); free(input); fprintf(stderr, "白噪声输出分配失败\n"); exit(2); }
        const size_t begin = prefix + 48000;
        const size_t end = count < written ? count : written;
        double in_power = 0.0, out_power = 0.0;
        for (size_t i = begin; i < end; i++) {
            in_power += (double)input[i] * input[i];
            out_power += (double)out[i] * out[i];
        }
        const double nr_db = 10.0 * log10(in_power / (out_power + 1e-30));
        printf("[ns] 白噪声前缀 %zu 样本, 初始噪声 %zu 样本, 稳态 NR %.2f dB\n",
               prefix, cases[test].initial_noise, nr_db);
        CHECK(end > begin);
        CHECK(nr_db >= WOLA_NR_GATE_DB);
        free(out);
        free(input);
        mozart_ns_free(ns);
    }
}

static void test_silence(void)
{
    const size_t count = 3 * 48000;
    float *input = calloc(count, sizeof(float));
    if (!input) { fprintf(stderr, "静音测试内存分配失败\n"); exit(2); }
    mozart_ns_t *ns = mozart_ns_new(&(mozart_ns_config_t){0});
    if (!ns) { free(input); fprintf(stderr, "降噪器创建失败\n"); exit(2); }
    size_t written = 0;
    float *out = process(input, count, &written, ns);
    if (!out) { mozart_ns_free(ns); free(input); fprintf(stderr, "静音输出分配失败\n"); exit(2); }
    CHECK(written >= count);
    CHECK(written <= count + 256);
    for (size_t i = 0; i < written; i++) {
        CHECK(isfinite(out[i]));
        CHECK(out[i] == 0.0f);
    }
    free(out);
    free(input);
    mozart_ns_free(ns);
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
    snprintf(path, sizeof(path), "%s/silence-input.f32", argv[1]);
    size_t silence_count = 0;
    float *silence_input = read_f32(path, &silence_count);
    snprintf(path, sizeof(path), "%s/silence-expected.f32", argv[1]);
    size_t silence_expected_count = 0;
    float *silence_expected = read_f32(path, &silence_expected_count);
    if (!input || !expected || !silence_input || !silence_expected) {
        fprintf(stderr, "夹具读取失败：%s\n", argv[1]);
        free(input); free(expected); free(silence_input); free(silence_expected);
        return 2;
    }

    mozart_ns_t *ns = mozart_ns_new(&(mozart_ns_config_t){0});
    if (!ns) { fprintf(stderr, "降噪器创建失败\n"); return 2; }
    size_t written = 0;
    float *out = process(input, in_count, &written, ns);
    if (!out) { fprintf(stderr, "输出分配失败\n"); return 2; }
    CHECK(written == expected_count);
    const size_t common = written < expected_count ? written : expected_count;
    double max_diff = 0.0;
    for (size_t i = 0; i < common; i++) {
        const double diff = fabs((double)out[i] - (double)expected[i]);
        if (diff > max_diff) max_diff = diff;
    }
    printf("[ns] 对拍 %zu 样本, 最大绝对差 %.3e\n", common, max_diff);
    // 门限依据：同配置下既有 C/Python 对拍门限为 1e-6。
    CHECK(max_diff < 1e-6);

    mozart_ns_t *silence_ns = mozart_ns_new(&(mozart_ns_config_t){0});
    if (!silence_ns) { fprintf(stderr, "静音夹具降噪器创建失败\n"); return 2; }
    size_t silence_written = 0;
    float *silence_out = process(silence_input, silence_count, &silence_written, silence_ns);
    if (!silence_out) { fprintf(stderr, "静音夹具输出分配失败\n"); return 2; }
    CHECK(silence_written == silence_expected_count);
    const size_t silence_common = silence_written < silence_expected_count
                                      ? silence_written : silence_expected_count;
    max_diff = 0.0;
    for (size_t i = 0; i < silence_common; i++) {
        const double diff = fabs((double)silence_out[i] - (double)silence_expected[i]);
        if (diff > max_diff) max_diff = diff;
    }
    printf("[ns] 静音前缀对拍 %zu 样本, 最大绝对差 %.3e\n", silence_common, max_diff);
    CHECK(max_diff < 1e-6);

    // 旁路保持既有恒等门槛和边界样本范围。
    mozart_ns_t *bypass = mozart_ns_new(&(mozart_ns_config_t){ .bypass = true });
    float *bout = calloc(in_count + 4096, sizeof(float));
    if (!bypass || !bout) { fprintf(stderr, "旁路测试内存分配失败\n"); return 2; }
    size_t bwritten = mozart_ns_push(bypass, input, in_count, bout, in_count + 4096);
    bwritten += mozart_ns_flush(bypass, bout + bwritten, in_count + 4096 - bwritten);
    double bmax = 0.0;
    for (size_t i = 8; i < in_count && i < bwritten; i++) {
        const double diff = fabs((double)bout[i] - (double)input[i]);
        if (diff > bmax) bmax = diff;
    }
    printf("[ns] bypass 最大绝对差 %.3e（跳过前 8 样本边界）\n", bmax);
    CHECK(bmax < 1e-6);

    // reset 后重新处理相同输入，逐样本复现首次输出。
    mozart_ns_reset(ns);
    size_t reset_written = 0;
    float *reset_out = process(input, in_count, &reset_written, ns);
    if (!reset_out) { fprintf(stderr, "reset 输出分配失败\n"); return 2; }
    CHECK(reset_written == written);
    for (size_t i = 0; i < written && i < reset_written; i++)
        CHECK(reset_out[i] == out[i]);

    test_white_noise_prefixes();
    test_silence();

    mozart_ns_t *tiny = mozart_ns_new(&(mozart_ns_config_t){0});
    if (!tiny) { fprintf(stderr, "遥测测试降噪器创建失败\n"); return 2; }
    float sink[256];
    mozart_ns_push(tiny, input, 2048, sink, sizeof(sink) / sizeof(sink[0]));
    const double conf = mozart_ns_speech_confidence(tiny);
    CHECK(conf >= 0.0 && conf <= 1.0);
    CHECK(mozart_ns_consumed(tiny) >= mozart_ns_emitted(tiny));
    CHECK(mozart_ns_new(&(mozart_ns_config_t){ .frame = 999 }) == NULL);

    mozart_ns_free(tiny);
    mozart_ns_free(ns);
    mozart_ns_free(silence_ns);
    mozart_ns_free(bypass);
    free(input); free(expected); free(out); free(silence_input);
    free(silence_expected); free(silence_out); free(bout); free(reset_out);
    if (failures) {
        fprintf(stderr, "test_noise_suppressor: %d 项失败\n", failures);
        return 1;
    }
    printf("[OK] test_noise_suppressor\n");
    return 0;
}
