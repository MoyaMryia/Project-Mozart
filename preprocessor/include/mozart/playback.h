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
 * 阻塞写一块浮点单声道 PCM（1..MOZART_OUTPUT_SAMPLES 帧）。
 * 内部转换为 S16_LE 立体声（L=R=mono），使用可取消的非阻塞 I/O 与有期限恢复。
 * 与 write_s16_direct 共享一把写锁，两个调用方可运行在不同线程。
 * @return 0 成功，-ECANCELED 表示停止请求，其他负数为 ALSA 或超时错误。
 */
int mozart_playback_write(mozart_playback_t *p, const float *pcm, size_t nframes);

/**
 * 直出路径：把交错 S16 立体声原样写入设备（不做任何转换）。
 * nframes 为每通道帧数，1..MOZART_OUTPUT_SAMPLES。
 * 返回值语义与 mozart_playback_write 相同。
 */
int mozart_playback_write_s16_direct(mozart_playback_t *p,
                                     const int16_t *stereo_s16, size_t nframes);

/** 实际生效的 period 帧数（open 后查询）。 */
unsigned mozart_playback_period_frames(const mozart_playback_t *p);

/** 累计 underrun（-EPIPE 恢复）次数。 */
long mozart_playback_underruns(const mozart_playback_t *p);

void mozart_playback_close(mozart_playback_t *p);

#ifdef __cplusplus
}
#endif

#endif // MOZART_PLAYBACK_H
