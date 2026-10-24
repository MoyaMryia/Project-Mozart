// dsp.h — 48 kHz 单干增强 + 16 kHz 契约帧打包
// ============================================================================
// 输入：48 kHz 交错 S16 立体声（实测两路为同信号副本，差 2 采样），
//       只取 FL 通道，避免取均值引入梳状滤波。
// 单干（M1）：FL 取样 → S16→f32 → 80 Hz 高通 → WOLA 谱降噪（noise_suppressor）
//       → 整形级（噪声门/AGC/限幅，默认关闭）。
// 打包：960 主干样本 → 3:1 FIR 降采样 → 16 kHz/320 样本契约帧 + meta。
// VAD：唯一来源 = WOLA 后验 SNR 置信度（noise_suppressor 提供）；
//       segment 双门限滞回（进入：连续 3 帧 ≥0.6；退出：连续 25 帧 <0.3）。
// 流式合同：mozart_dsp_enhance 支持任意块长，输出与输入索引对齐；
//           mozart_dsp_process 为 960 块的便捷包装（内部自动累积）。
#ifndef MOZART_DSP_H
#define MOZART_DSP_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "mozart/frame_meta.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct mozart_dsp mozart_dsp_t;

typedef struct {
    bool denoise;            // WOLA 谱降噪，默认 true；false 时该级旁路
    bool gate;               // 48 kHz 噪声门（默认 false，关闭时逐位恒等）
    bool agc;                // 48 kHz 自动增益（默认 false）
    bool limiter;            // 48 kHz 峰值限幅（默认 false）
} mozart_dsp_config_t;

/** 各级运行状态快照（control status 用）。 */
typedef struct {
    int denoise, gate, agc, limiter;      // 1 = 启用
    float gate_in_db, gate_out_db;
    long  gate_open, gate_close;
    float agc_gain_db, agc_in_db, agc_out_db;
    float limiter_in_db, limiter_out_db;
    float ns_last_gain;
    double ns_confidence;                 // 最近查询周期的语音置信度
} mozart_dsp_telemetry_t;

/** 创建。cfg 可为 NULL（全默认）。 */
mozart_dsp_t *mozart_dsp_new(const mozart_dsp_config_t *cfg);

/**
 * 单干增强：处理 n 个 48 kHz 单声道 f32 样本，向 out 写至多 out_cap 个样本。
 * 高通 → WOLA（可开关）→ 整形级。输出与输入索引对齐；启动期输出可能少于输入。
 * @return 实际写入样本数；参数非法返回 SIZE_MAX。
 */
size_t mozart_dsp_enhance(mozart_dsp_t *d, const float *in, size_t n,
                          float *out, size_t out_cap);

/**
 * 打包：960 个主干样本 → 契约帧。out->meta.pts_ns 由调用方预填。
 * @return 0 成功，负数失败。
 */
int mozart_dsp_pack_frame(mozart_dsp_t *d, const float *trunk_960,
                          mozart_input_frame_t *out);

/**
 * 便捷包装：960 帧 48 kHz 交错 S16 立体声 → 契约帧（内部经 enhance 累积）。
 * @return 0 成功，负数失败。
 */
int mozart_dsp_process(mozart_dsp_t *d,
                       const int16_t *stereo_s16,
                       mozart_input_frame_t *out);

/** 运行时切换各级开关（denoise 关闭 = WOLA 旁路）。 */
void mozart_dsp_set_stages(mozart_dsp_t *d,
                           bool denoise, bool gate, bool agc, bool limiter);

/** 读取各级遥测快照。d 为 NULL 时清零输出。 */
void mozart_dsp_telemetry(const mozart_dsp_t *d, mozart_dsp_telemetry_t *out);

/** 清空全部有状态（高通/WOLA/整形/VAD/分段/FIR 历史），frame_idx 归零。 */
void mozart_dsp_reset(mozart_dsp_t *d);

void mozart_dsp_free(mozart_dsp_t *d);

#ifdef __cplusplus
}
#endif

#endif // MOZART_DSP_H
