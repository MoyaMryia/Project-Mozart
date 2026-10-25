// async_src.h — 跨时钟域自适应重采样（异步采样率转换）
// ============================================================================
// 用途：吸收两个音频时钟域之间的漂移（±200 ppm 量级）。
// 原理：调用方维护一个小队列（跨域传输的缓冲），本模块按"队列水位/目标
// 水位"微调重采样比例——水位高（对端时钟快）就重采样得慢一点（拉伸消费），
// 水位低就快一点。比例被夹在 [1-ratio_max, 1+ratio_max]，默认 ±0.0005
//（±500 ppm，覆盖晶体漂移数十倍余量），听感不可闻。
// 输入输出均为 48 kHz f32 单声道帧序列（也适用于任意同采样率对）。
// 实现：线性插值（一阶），音频质量满足语音监控通道；主链路（直出/AI）
// 不经过本模块，无音质风险。
#ifndef MOZART_ASYNC_SRC_H
#define MOZART_ASYNC_SRC_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct mozart_src mozart_src_t;

/**
 * 创建。target_frames 为目标水位（帧）；ratio_max 为比例摆动上限。
 * 失败返回 NULL。
 */
mozart_src_t *mozart_src_new(double target_frames, double ratio_max);

/**
 * 推入 n 帧并报告当前应使用的重采样比例。
 * fill_ratio = 当前队列水位 / 目标水位（调用方从自己的队列测得），
 * 1.0 = 恰好；>1 表示对端偏快（应消费更慢 → 输出多于输入）。
 * 返回建议比例，写入 *ratio_out。
 */
double mozart_src_ratio(const mozart_src_t *src, double fill_ratio);

/**
 * 以 ratio 对 in[0..n) 重采样，输出写入 out（容量 out_cap）。
 * 内部维护相位（fractional read index）；输出数量由比例决定。
 * @return 实际写入帧数（0..n），-1 失败。
 */
int mozart_src_process(mozart_src_t *src, const float *in, int n,
                       float *out, int out_cap, double ratio);

/** 遥测：当前相位与累计比例均值。 */
double mozart_src_avg_ratio(const mozart_src_t *src);

void mozart_src_free(mozart_src_t *src);

#ifdef __cplusplus
}
#endif

#endif // MOZART_ASYNC_SRC_H
