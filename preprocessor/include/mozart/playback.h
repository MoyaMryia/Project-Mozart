// playback.h — ALSA 播放（48 kHz / S16_LE / 2ch）
// ============================================================================
// 用于 mozart-pre 的"收回包"路径：接收后端 UDP 输出契约帧（48 kHz / f32 / 960
// 样本），转换为 S16_LE 立体声并写入本地声卡。与 capture.c 对称。
#ifndef MOZART_PLAYBACK_H
#define MOZART_PLAYBACK_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct mozart_playback mozart_playback_t;

typedef struct {
    const char *device;        // 如 "plughw:1,3" / "default"
    unsigned    rate;          // 48000
    unsigned    channels;      // 2
    unsigned    period_frames; // 960 (20 ms)
    unsigned    n_periods;     // buffer = period * n_periods，默认 4
} mozart_playback_config_t;

/** 打开播放设备。失败返回 NULL（错误信息打印到 stderr）。 */
mozart_playback_t *mozart_playback_open(const mozart_playback_config_t *cfg);

/**
 * 阻塞写一 period 的浮点单声道 PCM（nframes 应等于 period_frames）。
 * 内部转换为 S16_LE 立体声（L=R=mono），处理 xrun（-EPIPE）与恢复。
 * @return 0 成功，负数失败。
 */
int mozart_playback_write(mozart_playback_t *p, const float *pcm, size_t nframes);

/** 累计 underrun（-EPIPE 恢复）次数。 */
long mozart_playback_underruns(const mozart_playback_t *p);

void mozart_playback_close(mozart_playback_t *p);

#ifdef __cplusplus
}
#endif

#endif // MOZART_PLAYBACK_H
