// playback.h — ALSA 播放（48 kHz / S16_LE / 2ch）
// ============================================================================
// 用于 mozart-pre 的"收回包"路径：接收后端 UDP 输出契约帧（48 kHz / f32 / 960
// 样本），转换为 S16_LE 立体声并写入本地声卡。与 capture.c 对称。
#ifndef MOZART_PLAYBACK_H
#define MOZART_PLAYBACK_H

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

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
    bool      (*should_stop)(void *); // 在等待及恢复期间查询停止请求
    void       *stop_context;
} mozart_playback_config_t;

/** 打开播放设备。失败返回 NULL（错误信息打印到 stderr）。 */
mozart_playback_t *mozart_playback_open(const mozart_playback_config_t *cfg);

/**
 * 阻塞写一 period 的浮点单声道 PCM（nframes 应等于 period_frames）。
 * 内部转换为 S16_LE 立体声（L=R=mono），使用可取消的非阻塞 I/O 与有期限恢复。
 * @return 0 成功，-ECANCELED 表示停止请求，其他负数为 ALSA 或超时错误。
 */
int mozart_playback_write(mozart_playback_t *p, const float *pcm, size_t nframes);

/** 累计 underrun（-EPIPE 恢复）次数。 */
long mozart_playback_underruns(const mozart_playback_t *p);

void mozart_playback_close(mozart_playback_t *p);

#ifdef __cplusplus
}
#endif

#endif // MOZART_PLAYBACK_H
