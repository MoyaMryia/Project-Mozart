// dtln.c — DTLN ONNX 链 C 实现（M5）
// ============================================================================
// 上游 real_time_processing_onnx.py 的逐行移植，使用 ONNX Runtime C API。
// 索引对齐：process 输入按 128 样本泵；首个块的前 384 样本（block_len−shift）
// 为收敛填充，输出缓冲按序弹出，与 Python 参考验证脚本的 latency 口径一致。
#define _POSIX_C_SOURCE 200809L
#include "mozart/dtln.h"

#include <onnxruntime_c_api.h>

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define DTLN_BLOCK_LEN 512
#define DTLN_BLOCK_SHIFT 128
#define DTLN_BINS 257
#define DTLN_STATES 512    // 1*2*128*2 = 512 个 LSTM 状态元素
#define DTLN_FILL (DTLN_BLOCK_LEN - DTLN_BLOCK_SHIFT)
#define DTLN_PI 3.14159265358979323846

struct mozart_dtln {
    const OrtApi *api;
    OrtEnv *env;
    OrtSession *s1, *s2;
    OrtMemoryInfo *memory;

    float in_buffer[DTLN_BLOCK_LEN];
    float out_buffer[DTLN_BLOCK_LEN];
    float states_1[DTLN_STATES];
    float states_2[DTLN_STATES];
    float pending[DTLN_BLOCK_LEN];    // 待发射的 out_buffer 增量
    size_t pending_len;

    double us_per_block;
    int    blocks;
};

static double now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1e6 + (double)ts.tv_nsec / 1e3;
}

static OrtSession *load_session(mozart_dtln_t *dt, const char *path, int threads)
{
    OrtSessionOptions *options = NULL;
    OrtStatus *status = dt->api->CreateSessionOptions(&options);
    if (status) goto fail;
    dt->api->SetIntraOpNumThreads(options, threads);
    dt->api->SetInterOpNumThreads(options, 1);
    OrtSession *session = NULL;
    status = dt->api->CreateSession(dt->env, path, options, &session);
    dt->api->ReleaseSessionOptions(options);
    if (status) {
        goto fail;
    }
    return session;
fail:
    if (status) {
        const char *message = dt->api->GetErrorMessage(status);
        fprintf(stderr, "[dtln] 加载 %s 失败: %s\n", path, message ? message : "?");
        dt->api->ReleaseStatus(status);
    }
    return NULL;
}

mozart_dtln_t *mozart_dtln_new(const char *model1, const char *model2, int threads)
{
    if (!model1 || !model2) return NULL;
    mozart_dtln_t *dt = calloc(1, sizeof(*dt));
    if (!dt) return NULL;
    dt->api = OrtGetApiBase()->GetApi(23);   // 与板端 1.27/本机 1.23 共同支持的版本
    if (!dt->api) { free(dt); return NULL; }
    OrtStatus *status = dt->api->CreateEnv(ORT_LOGGING_LEVEL_WARNING, "mozart-dtln", &dt->env);
    if (status) {
        dt->api->ReleaseStatus(status);
        free(dt);
        return NULL;
    }
    status = dt->api->CreateCpuMemoryInfo(OrtArenaAllocator, OrtMemTypeDefault, &dt->memory);
    if (status) {
        dt->api->ReleaseStatus(status);
        dt->api->ReleaseEnv(dt->env);
        free(dt);
        return NULL;
    }
    dt->s1 = load_session(dt, model1, threads);
    dt->s2 = load_session(dt, model2, threads);
    if (!dt->s1 || !dt->s2) {
        mozart_dtln_free(dt);
        return NULL;
    }
    return dt;
}

// 用 value 填充 4 维状态张量并创建会话输入
static OrtValue *make_state(mozart_dtln_t *dt, const float *value)
{
    const int64_t shape[4] = {1, 2, 128, 2};
    OrtAllocator *allocator = NULL;
    OrtStatus *alloc_status = dt->api->GetAllocatorWithDefaultOptions(&allocator);
    if (alloc_status) {
        dt->api->ReleaseStatus(alloc_status);
        return NULL;
    }
    OrtValue *tensor = NULL;
    OrtStatus *status = dt->api->CreateTensorAsOrtValue(
        allocator, shape, 4, ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &tensor);
    if (status) {
        dt->api->ReleaseStatus(status);
        return NULL;
    }
    float *data = NULL;
    status = dt->api->GetTensorMutableData(tensor, (void **)&data);
    if (status) {
        dt->api->ReleaseStatus(status);
        dt->api->ReleaseValue(tensor);
        return NULL;
    }
    memcpy(data, value, sizeof(float) * DTLN_STATES);
    return tensor;
}

static OrtValue *make_tensor(mozart_dtln_t *dt, float *data,
                             const int64_t *shape, size_t rank)
{
    int64_t total = 1;
    for (size_t i = 0; i < rank; i++) total *= shape[i];
    OrtValue *tensor = NULL;
    OrtStatus *status = dt->api->CreateTensorWithDataAsOrtValue(
        dt->memory, data, sizeof(float) * total, shape, rank,
        ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &tensor);
    if (status) {
        dt->api->ReleaseStatus(status);
        return NULL;
    }
    return tensor;
}

// 单个 128 样本块的完整推理（模型 1 + 模型 2），返回 0 成功
static int dtln_infer_block(mozart_dtln_t *dt)
{
    double t0 = now_us();

    // ---- model_1：magnitude mask ----
    float spec_real[DTLN_BINS], spec_imag[DTLN_BINS], mag[DTLN_BINS];
    for (int b = 0; b < DTLN_BINS; b++) {
        // 与 numpy rfft 一致：实部/虚部由 DFT 定义；用递推不划算，直接小规模 DFT
        // （257 bins × 512 点每块 ≈ 0.13 MFLOP，RTF 余量内；板端实测 0.124 含此开销）
        float re = 0.0f, im = 0.0f;
        for (int k = 0; k < DTLN_BLOCK_LEN; k++) {
            const double angle = -2.0 * DTLN_PI * (double)b * (double)k / (double)DTLN_BLOCK_LEN;
            re += (float)(dt->in_buffer[k] * cos(angle));
            im += (float)(dt->in_buffer[k] * sin(angle));
        }
        spec_real[b] = re;
        spec_imag[b] = im;
        mag[b] = sqrtf(re * re + im * im);
    }

    const int64_t mag_shape[3] = {1, 1, DTLN_BINS};
    OrtValue *mag_tensor = make_tensor(dt, mag, mag_shape, 3);
    OrtValue *state1 = make_state(dt, dt->states_1);
    if (!mag_tensor || !state1) return -1;
    OrtValue *inputs1[2] = {mag_tensor, state1};
    const char *names1[2] = {"input_2", "input_3"};
    const char *out_names1[2] = {"activation_2", "tf_op_layer_stack_2"};
    OrtValue *outputs1[2] = {NULL, NULL};
    OrtStatus *status = dt->api->Run(dt->s1, NULL, names1, inputs1, 2,
                                     out_names1, 2, outputs1);
    dt->api->ReleaseValue(mag_tensor);
    dt->api->ReleaseValue(state1);
    if (status) {
        const char *message = dt->api->GetErrorMessage(status);
        fprintf(stderr, "[dtln] model_1 Run 失败: %s\n", message ? message : "?");
        dt->api->ReleaseStatus(status);
        return -1;
    }
    float *mask = NULL, *new_state1 = NULL;
    dt->api->GetTensorMutableData(outputs1[0], (void **)&mask);
    dt->api->GetTensorMutableData(outputs1[1], (void **)&new_state1);
    memcpy(dt->states_1, new_state1, sizeof(float) * DTLN_STATES);

    // mask 谱 → 时域块。厄米对称重建（等价 numpy irfft）：
    // x[k] = (1/N) [ X0 + 2·Σ_{b=1}^{N/2-1} Re(Xb·e^{iθ}) + Re(X_{N/2}·e^{iπk}) ]
    float block[DTLN_BLOCK_LEN];
    for (int k = 0; k < DTLN_BLOCK_LEN; k++) {
        float acc = mask[0] * spec_real[0];
        for (int b = 1; b < DTLN_BINS - 1; b++) {
            const double angle = 2.0 * DTLN_PI * (double)b * (double)k / (double)DTLN_BLOCK_LEN;
            const float mr = mask[b] * spec_real[b];
            const float mi = mask[b] * spec_imag[b];
            acc += 2.0f * (float)(mr * cos(angle) - mi * sin(angle));
        }
        // bin N/2：sin(πk) 恒为 0
        acc += mask[DTLN_BINS - 1] * spec_real[DTLN_BINS - 1]
               * (float)cos(DTLN_PI * (double)k);
        block[k] = acc / (float)DTLN_BLOCK_LEN;
    }
    dt->api->ReleaseValue(outputs1[0]);
    dt->api->ReleaseValue(outputs1[1]);

    // ---- model_2：时域特征域增益 ----
    const int64_t block_shape[3] = {1, 1, DTLN_BLOCK_LEN};
    OrtValue *block_tensor = make_tensor(dt, block, block_shape, 3);
    OrtValue *state2 = make_state(dt, dt->states_2);
    if (!block_tensor || !state2) return -1;
    OrtValue *inputs2[2] = {block_tensor, state2};
    const char *names2[2] = {"input_4", "input_5"};
    const char *out_names2[2] = {"conv1d_3", "tf_op_layer_stack_5"};
    OrtValue *outputs2[2] = {NULL, NULL};
    status = dt->api->Run(dt->s2, NULL, names2, (const OrtValue *const *)inputs2, 2,
                          out_names2, 2, outputs2);
    dt->api->ReleaseValue(block_tensor);
    dt->api->ReleaseValue(state2);
    if (status) {
        const char *message = dt->api->GetErrorMessage(status);
        fprintf(stderr, "[dtln] model_2 Run 失败: %s\n", message ? message : "?");
        dt->api->ReleaseStatus(status);
        return -1;
    }
    float *out_block = NULL, *new_state2 = NULL;
    dt->api->GetTensorMutableData(outputs2[0], (void **)&out_block);
    dt->api->GetTensorMutableData(outputs2[1], (void **)&new_state2);
    memcpy(dt->states_2, new_state2, sizeof(float) * DTLN_STATES);

    // 上游 out_buffer 滚动叠加，弹出一 hop
    memmove(dt->out_buffer, dt->out_buffer + DTLN_BLOCK_SHIFT,
            sizeof(float) * (DTLN_BLOCK_LEN - DTLN_BLOCK_SHIFT));
    memset(dt->out_buffer + DTLN_BLOCK_LEN - DTLN_BLOCK_SHIFT, 0,
           sizeof(float) * DTLN_BLOCK_SHIFT);
    for (int i = 0; i < DTLN_BLOCK_LEN; i++)
        dt->out_buffer[i] += out_block[i];

    dt->api->ReleaseValue(outputs2[0]);
    dt->api->ReleaseValue(outputs2[1]);

    const double took = now_us() - t0;
    dt->us_per_block = dt->us_per_block * 0.9 + took * 0.1;
    dt->blocks++;
    return 0;
}

int mozart_dtln_push(mozart_dtln_t *dt, const float *in, int n,
                     float *out, int out_cap)
{
    if (!dt || !in || n < 0 || (n % DTLN_BLOCK_SHIFT) != 0) return -1;
    int written = 0;
    for (int offset = 0; offset < n; offset += DTLN_BLOCK_SHIFT) {
        memmove(dt->in_buffer, dt->in_buffer + DTLN_BLOCK_SHIFT,
                sizeof(float) * (DTLN_BLOCK_LEN - DTLN_BLOCK_SHIFT));
        memcpy(dt->in_buffer + DTLN_BLOCK_LEN - DTLN_BLOCK_SHIFT,
               in + offset, sizeof(float) * DTLN_BLOCK_SHIFT);
        if (dtln_infer_block(dt) < 0) return -1;
        // 与上游一致：每块都发射 out_buffer 头部 shift 样本。
        // 流式延迟 = block_len - block_shift = 384 样本，调用方按需对齐。
        for (int i = 0; i < DTLN_BLOCK_SHIFT; i++) {
            if (written < out_cap) out[written++] = dt->out_buffer[i];
        }
    }
    return written;
}

double mozart_dtln_us_per_block(const mozart_dtln_t *dt)
{
    return dt ? dt->us_per_block : 0.0;
}

void mozart_dtln_free(mozart_dtln_t *dt)
{
    if (!dt) return;
    if (dt->s1) dt->api->ReleaseSession(dt->s1);
    if (dt->s2) dt->api->ReleaseSession(dt->s2);
    if (dt->memory) dt->api->ReleaseMemoryInfo(dt->memory);
    if (dt->env) dt->api->ReleaseEnv(dt->env);
    free(dt);
}
