// capture.h — ALSA 采集（48 kHz / S16_LE / 2ch / 等时 ASYNC 端点）
// ============================================================================
// 麦克风仅提供一种格式：S16_LE、双通道、48000 Hz、等时端点 ASYNC（全速）。
// 本模块按 hw: 精确参数打开设备，阻塞式按 period（默认 960 帧 = 20 ms）读取。
#ifndef MOZART_CAPTURE_H
#define MOZART_CAPTURE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct mozart_capture mozart_capture_t;

typedef struct {
    const char *device;        // 如 "hw:2,0" / "plughw:2,0" / "default"
    unsigned    rate;          // 48000
    unsigned    channels;      // 2
    unsigned    period_frames; // 960 (20 ms)
    unsigned    n_periods;     // buffer = period * n_periods，默认 4
} mozart_capture_config_t;

/** 打开设备并启动采集。失败返回 NULL（错误信息打印到 stderr）。 */
mozart_capture_t *mozart_capture_open(const mozart_capture_config_t *cfg);

/**
 * 阻塞读一 period 到 buf（交错 S16，frames*channels 个样本）。
 * 内部处理 overrun（-EPIPE）与恢复，连续失败超过上限返回负数。
 * @return 0 成功。
 */
int mozart_capture_read(mozart_capture_t *c, int16_t *buf);

/** CLOCK_MONOTONIC 纳秒时间戳（采集侧统一时钟）。 */
uint64_t mozart_now_ns(void);

/** 累计 overrun（-EPIPE 恢复）次数。 */
long mozart_capture_overruns(const mozart_capture_t *c);

void mozart_capture_close(mozart_capture_t *c);

#ifdef __cplusplus
}
#endif

#endif // MOZART_CAPTURE_H
