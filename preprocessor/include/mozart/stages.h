// stages.h — 48 kHz 可选处理级：噪声门 / AGC / 峰值限幅
// ============================================================================
// 每级独立开关（enabled=false 时 process 立即返回，样本逐位不变）。
// 全部工作在 48 kHz 单声道 f32 块上、就地处理；块长任意（dsp.c 固定 960）。
// 遥测字段（*_db / 计数）由 process 刷新，供状态接口快照使用。
#ifndef MOZART_STAGES_H
#define MOZART_STAGES_H

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool  enabled;
    float threshold_db;    // 开门限：块 RMS 高于此值开门
    float attack_ms;       // 增益 0→1 上升时长
    float hold_ms;         // 电平回落后保持开门的时长
    float release_ms;      // 增益 1→0 下降时长
    // 运行状态
    bool  open;
    int   hold_blocks;     // 剩余保持块数（以 20 ms 块计）
    float gain;            // 当前增益 0..1
    long  open_count, close_count;
    float in_peak_db, out_peak_db;
} mozart_gate_t;

typedef struct {
    bool  enabled;
    float target_db;       // 目标输出 RMS 电平
    float max_gain_db;     // 增益上限（只放大不衰减）
    float up_coeff;        // 增益上升的每块平滑系数（慢）
    float down_coeff;      // 增益下降的每块平滑系数（快）
    float gain;            // 当前增益 dB
    float in_rms_db, out_rms_db;
} mozart_agc_t;

typedef struct {
    bool  enabled;
    float ceiling_db;      // 输出峰值上限
    float release_ms;      // 包络与增益恢复时长
    // 运行状态
    float env;             // 峰值包络（立即上、指数下）
    float gain;            // 当前增益 ≤1
    float in_peak_db, out_peak_db;
} mozart_limiter_t;

/** 按出厂默认初始化各级（rate 用于把毫秒换算为样本系数）。 */
void mozart_gate_init(mozart_gate_t *g, unsigned rate);
void mozart_agc_init(mozart_agc_t *a, unsigned rate);
void mozart_limiter_init(mozart_limiter_t *l, unsigned rate);

/** 就地处理一块；enabled=false 时无操作。 */
void mozart_gate_process(mozart_gate_t *g, float *x, size_t n, unsigned rate);
void mozart_agc_process(mozart_agc_t *a, float *x, size_t n, unsigned rate);
void mozart_limiter_process(mozart_limiter_t *l, float *x, size_t n, unsigned rate);

/** 块峰值 / RMS 电平（dBFS；静音返回 -120.0）。 */
float mozart_peak_db(const float *x, size_t n);
float mozart_rms_db(const float *x, size_t n);

#ifdef __cplusplus
}
#endif

#endif // MOZART_STAGES_H
