// dsp.h — 采集帧 → 16k 契约帧的 DSP 链
// ============================================================================
// 输入：48 kHz 交错 S16 立体声（实测两路为同信号副本，差 2 采样），
//       只取 FL 通道，避免取均值引入梳状滤波。
// 链路：FL 取样 → S16→f32 (÷32768) → 80 Hz 高通 → RNNoise（全湿）
//       → 3:1 FIR 降采样 → 16 kHz/320 样本契约帧 + meta 填充。
// VAD：单一只来源 = RNNoise 语音概率；segment 用双门限滞回
//       （进入：连续 3 帧 ≥0.6；退出：连续 25 帧 <0.3）。
#ifndef MOZART_DSP_H
#define MOZART_DSP_H

#include <stdbool.h>
#include <stdint.h>
#include "mozart/frame_meta.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct mozart_dsp mozart_dsp_t;

typedef struct {
    bool rnnoise;            // 默认 true；false 时该级直通（VAD 退化为能量门限）
    const char *rnnoise_model; // NULL = 内置模型
} mozart_dsp_config_t;

/** 创建 DSP 链。cfg 可为 NULL（全默认）。 */
mozart_dsp_t *mozart_dsp_new(const mozart_dsp_config_t *cfg);

/**
 * 处理一帧：960 帧 48 kHz 交错 S16 立体声 → 契约帧。
 * out->meta.pts_ns 由调用方预填（采集时刻），其余字段由本函数填写。
 * @return 0 成功，负数失败。
 */
int mozart_dsp_process(mozart_dsp_t *d,
                       const int16_t *stereo_s16,
                       mozart_input_frame_t *out);

/** 清空全部有状态（高通/RNNoise/VAD/分段/FIR 历史），frame_idx 归零。 */
void mozart_dsp_reset(mozart_dsp_t *d);

void mozart_dsp_free(mozart_dsp_t *d);

#ifdef __cplusplus
}
#endif

#endif // MOZART_DSP_H
