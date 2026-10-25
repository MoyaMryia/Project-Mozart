// dtln.h — DTLN 语音增强（AI 分支，16 kHz 域，ONNX Runtime）
// ============================================================================
// 契约与上游 real_time_processing_onnx.py 一致：block_len 512、block_shift
// 128（75% 重叠）、model_1 magnitude mask + model_2 时域增益、LSTM 状态跨帧。
// 挂点：3:1 降采样之后、契约帧打包之前（16 kHz 域）。
// 契约帧 320 样本与 block_shift 128 不整除：内部按 128 泵 + 索引对齐缓冲，
// push 任意长度、输出与输入对齐（与 WOLA 同模式）。
// 权重与许可：rvc-golden/dtln/model_{1,2}.onnx（上游 MIT）。
#ifndef MOZART_DTLN_H
#define MOZART_DTLN_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct mozart_dtln mozart_dtln_t;

/**
 * 打开链。model1/model2 为 ONNX 路径；任一加载失败返回 NULL（原因打印）。
 * threads 为单会话线程数（板端预算按单核 1 线程标定）。
 */
mozart_dtln_t *mozart_dtln_new(const char *model1, const char *model2, int threads);

/**
 * 流式处理 n 个 16 kHz f32 样本，写至多 out_cap 个到 out。
 * 输出与输入索引对齐（启动期前 384 样本为收敛填充）。
 * @return 实际写入样本数，-1 失败。
 */
int mozart_dtln_push(mozart_dtln_t *dt, const float *in, int n,
                     float *out, int out_cap);

/** 处理耗时指数均值（微秒/128 样本块），遥测用。 */
double mozart_dtln_us_per_block(const mozart_dtln_t *dt);

void mozart_dtln_free(mozart_dtln_t *dt);

#ifdef __cplusplus
}
#endif

#endif // MOZART_DTLN_H
