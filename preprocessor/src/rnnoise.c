// rnnoise.c — xiph RNNoise 包装（纯 API）
// ============================================================================
// 内部完成 ±32768 缩放：RNNoise 期望 int16 电平范围的输入，
// 调用方传 ±1.0 归一化 float。输出策略为全湿（100% 降噪）。
#ifndef MOZART_USE_RNNOISE
// ---- 无 RNNoise 构建时：直通桩，VAD 概率恒 0 ------------------------------
#include "mozart/rnnoise.h"
#include <stdlib.h>
#include <string.h>

#define RNNOISE_FRAME_SAMPLES 480

struct mozart_rnnoise { int unused; };

mozart_rnnoise_t *mozart_rnnoise_new(const char *model_path)
{
    (void)model_path;
    return calloc(1, sizeof(mozart_rnnoise_t));
}

int mozart_rnnoise_process(mozart_rnnoise_t *rn, const float *in,
                           float *out, float *vad_prob)
{
    (void)rn;
    if (!in || !out) return -1;
    memcpy(out, in, RNNOISE_FRAME_SAMPLES * sizeof(float));
    if (vad_prob) *vad_prob = 0.0f;
    return 0;
}

void mozart_rnnoise_reset(mozart_rnnoise_t *rn) { (void)rn; }
void mozart_rnnoise_free(mozart_rnnoise_t *rn) { free(rn); }

#else // MOZART_USE_RNNOISE

#include "mozart/rnnoise.h"
#include "rnnoise.h" // xiph 公共头
#include <stdlib.h>
#include <string.h>

#ifndef MOZART_RNNOISE_DEFAULT_MODEL
#define MOZART_RNNOISE_DEFAULT_MODEL "assets/rnnoise_default.rnnb"
#endif

#define RNNOISE_FRAME_SAMPLES 480
#define PCM16_SCALE 32768.0f

struct mozart_rnnoise {
    DenoiseState *state;
    RNNModel     *model;   // blob 句柄，destroy 时释放
    FILE         *blob_file; // rnnoise_model_free 不关我们开的 FILE，自己管
};

// 解析模型路径：显式传入 > 环境变量 MOZART_RNNOISE_MODEL > 编译期内置默认
static const char *resolve_model_path(const char *explicit_path)
{
    if (explicit_path) return explicit_path;
    const char *env = getenv("MOZART_RNNOISE_MODEL");
    if (env && env[0]) return env;
    return MOZART_RNNOISE_DEFAULT_MODEL;
}

mozart_rnnoise_t *mozart_rnnoise_new(const char *model_path)
{
    mozart_rnnoise_t *rn = calloc(1, sizeof(*rn));
    if (!rn) return NULL;

    // USE_WEIGHTS_FILE 构建下权重只在 blob 文件里，必须成功加载。
    // 不用 rnnoise_model_from_filename：上游不检查 fopen 失败（会段错误）。
    const char *path = resolve_model_path(model_path);
    rn->blob_file = fopen(path, "rb");
    if (!rn->blob_file) {
        fprintf(stderr, "[rnnoise] 无法打开模型 blob: %s\n"
                        "  （重新生成: cmake --build build --target export_rnnoise_blob）\n",
                path);
        free(rn);
        return NULL;
    }
    rn->model = rnnoise_model_from_file(rn->blob_file);
    if (!rn->model) {
        fprintf(stderr, "[rnnoise] 模型 blob 读取失败: %s\n", path);
        fclose(rn->blob_file);
        free(rn);
        return NULL;
    }

    rn->state = rnnoise_create(rn->model);
    if (!rn->state) {
        rnnoise_model_free(rn->model);
        fclose(rn->blob_file);
        free(rn);
        return NULL;
    }
    return rn;
}

int mozart_rnnoise_process(mozart_rnnoise_t *rn, const float *in,
                           float *out, float *vad_prob)
{
    if (!rn || !in || !out) return -1;
    float scaled_in[RNNOISE_FRAME_SAMPLES];
    for (int i = 0; i < RNNOISE_FRAME_SAMPLES; i++)
        scaled_in[i] = in[i] * PCM16_SCALE;

    float p = rnnoise_process_frame(rn->state, out, scaled_in);
    for (int i = 0; i < RNNOISE_FRAME_SAMPLES; i++)
        out[i] /= PCM16_SCALE;

    if (vad_prob) {
        if (p < 0.0f) p = 0.0f;
        if (p > 1.0f) p = 1.0f;
        *vad_prob = p;
    }
    return 0;
}

void mozart_rnnoise_reset(mozart_rnnoise_t *rn)
{
    if (!rn || !rn->state) return;
    RNNModel *m = rn->model;
    rnnoise_destroy(rn->state);
    rn->state = rnnoise_create(m);
}

void mozart_rnnoise_free(mozart_rnnoise_t *rn)
{
    if (!rn) return;
    if (rn->state) rnnoise_destroy(rn->state);
    if (rn->model) rnnoise_model_free(rn->model);
    if (rn->blob_file) fclose(rn->blob_file);
    free(rn);
}

#endif // MOZART_USE_RNNOISE
