// rnnoise.h — RNNoise 降噪器（纯 API，无 stage 抽象）
// ============================================================================
// RNNoise 原生工作在 48 kHz / 10 ms（480 样本）帧。本模块内部完成
// ±32768 缩放（RNNoise 期望 int16 电平范围），调用方直接传 ±1.0 归一化
// float。
//
// 输出策略为"全湿"：直接输出 100% 降噪信号，不做 wet/dry 混合。
#ifndef MOZART_RNNOISE_H
#define MOZART_RNNOISE_H

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct mozart_rnnoise mozart_rnnoise_t;

/** 创建降噪器。model_path 为 NULL 时使用内置默认模型。失败返回 NULL。 */
mozart_rnnoise_t *mozart_rnnoise_new(const char *model_path);

/**
 * 处理一帧（480 样本 @48 kHz）。
 * in/out 可指向同一缓冲（原地处理）。vad_prob 输出 [0,1] 语音概率，可为 NULL。
 * @return 0 成功，负数失败。
 */
int mozart_rnnoise_process(mozart_rnnoise_t *rn, const float *in,
                           float *out, float *vad_prob);

/** 清空内部 GRU/FIR 状态（segment 切换时调用）。 */
void mozart_rnnoise_reset(mozart_rnnoise_t *rn);

void mozart_rnnoise_free(mozart_rnnoise_t *rn);

#ifdef __cplusplus
}
#endif

#endif // MOZART_RNNOISE_H
