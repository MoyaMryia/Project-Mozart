// playback.c — ALSA 播放（48 kHz / S16_LE / 2ch）
// ============================================================================
// 接收后端输出的 48 kHz / f32 / 960 样本单声道帧，转换为 S16_LE 立体声
// （L=R=mono）后写入 ALSA。非阻塞 I/O 支持停止请求与有期限恢复。
#define _POSIX_C_SOURCE 200809L
#include "mozart/playback.h"
#include "mozart/frame_meta.h"

#include <alloca.h>
#include <alsa/asoundlib.h>
#include <pthread.h>
#include <stdatomic.h>
#include "pcm_io.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

struct mozart_playback {
    snd_pcm_t   *pcm;
    unsigned     channels;
    unsigned     period_frames;
    atomic_long  underruns;
    pthread_mutex_t write_mutex;  // 回包线程与直出线程共用设备，串行化写入
    bool       (*should_stop)(void *);
    void        *stop_context;
};

mozart_playback_t *mozart_playback_open(const mozart_playback_config_t *cfg)
{
    if (!cfg || !cfg->device) return NULL;

    mozart_playback_t *p = calloc(1, sizeof(*p));
    if (!p) return NULL;
    p->channels      = cfg->channels ? cfg->channels : 2;
    p->period_frames = cfg->period_frames ? cfg->period_frames : 960;
    p->should_stop = cfg->should_stop;
    p->stop_context = cfg->stop_context;
    if (pthread_mutex_init(&p->write_mutex, NULL) != 0) {
        free(p);
        return NULL;
    }

    snd_pcm_t *pcm = NULL;
    int dir = 0;
    int err = snd_pcm_open(&pcm, cfg->device, SND_PCM_STREAM_PLAYBACK, SND_PCM_NONBLOCK);
    if (err < 0) {
        fprintf(stderr, "[playback] open(%s): %s\n",
                cfg->device, snd_strerror(err));
        free(p);
        return NULL;
    }

    snd_pcm_hw_params_t *hw;
    snd_pcm_hw_params_alloca(&hw);
    snd_pcm_hw_params_any(pcm, hw);

    err = snd_pcm_hw_params_set_access(pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED);
    if (err >= 0) err = snd_pcm_hw_params_set_format(pcm, hw, SND_PCM_FORMAT_S16_LE);
    if (err >= 0) err = snd_pcm_hw_params_set_channels(pcm, hw, p->channels);
    if (err == 0) {
        unsigned rate = cfg->rate;
        err = snd_pcm_hw_params_set_rate_near(pcm, hw, &rate, &dir);
        if (err == 0 && rate != cfg->rate) {
            fprintf(stderr, "[playback] device gave %u Hz, want %u\n",
                    rate, cfg->rate);
            err = -1;
        }
    }
    if (err == 0) {
        snd_pcm_uframes_t period = p->period_frames;
        // 缓冲至少容纳一个 AI 路径整帧（960）加一个 period 余量，
        // 使小 period 直出与 960 帧回包写入共用同一设备。
        snd_pcm_uframes_t buffer = period * (cfg->n_periods ? cfg->n_periods : 4);
        if (buffer < period + MOZART_OUTPUT_SAMPLES)
            buffer = period + MOZART_OUTPUT_SAMPLES;
        err = snd_pcm_hw_params_set_period_size_near(pcm, hw, &period, &dir);
        if (err == 0)
            err = snd_pcm_hw_params_set_buffer_size_near(pcm, hw, &buffer);
        if (err == 0) p->period_frames = (unsigned)period;
    }
    if (err == 0) err = snd_pcm_hw_params(pcm, hw);
    if (err < 0) {
        fprintf(stderr, "[playback] hw_params: %s\n", snd_strerror(err));
        snd_pcm_close(pcm);
        free(p);
        return NULL;
    }

    // 软件参数：尽量低启动延迟
    snd_pcm_sw_params_t *sw;
    snd_pcm_sw_params_alloca(&sw);
    snd_pcm_sw_params_current(pcm, sw);
    snd_pcm_sw_params_set_start_threshold(pcm, sw, 1);
    snd_pcm_sw_params(pcm, sw);

    err = snd_pcm_prepare(pcm);
    if (err < 0) {
        fprintf(stderr, "[playback] prepare: %s\n", snd_strerror(err));
        snd_pcm_close(pcm);
        free(p);
        return NULL;
    }

    p->pcm = pcm;
    return p;
}


static void float_to_s16_stereo(const float *src, int16_t *dst, size_t n)
{
    for (size_t i = 0; i < n; ++i) {
        float v = src[i];
        if (v > 1.0f) v = 1.0f;
        else if (v < -1.0f) v = -1.0f;
        const int16_t s = (int16_t)(v * 32767.0f);
        dst[2 * i]     = s;
        dst[2 * i + 1] = s;
    }
}

int mozart_playback_write(mozart_playback_t *p, const float *pcm, size_t nframes)
{
    if (!p || !pcm || nframes == 0 || nframes > MOZART_OUTPUT_SAMPLES) return -1;

    // 调用方（回包线程）固定传入 MOZART_OUTPUT_SAMPLES；直出线程按 period 写 S16。
    int16_t buf[MOZART_OUTPUT_SAMPLES * 2];
    float_to_s16_stereo(pcm, buf, nframes);

    pthread_mutex_lock(&p->write_mutex);
    const int result = pcm_transfer(p->pcm, buf, nframes, p->channels,
                                    false, p->should_stop, p->stop_context, &p->underruns);
    pthread_mutex_unlock(&p->write_mutex);
    if (result < 0 && result != -ECANCELED)
        fprintf(stderr, "[playback] write: %s\n", snd_strerror(result));
    return result;
}

int mozart_playback_write_s16_direct(mozart_playback_t *p,
                                     const int16_t *stereo_s16, size_t nframes)
{
    if (!p || !stereo_s16 || nframes == 0 || nframes > MOZART_OUTPUT_SAMPLES) return -1;

    pthread_mutex_lock(&p->write_mutex);
    const int result = pcm_transfer(p->pcm, (int16_t *)stereo_s16, nframes,
                                    p->channels, false,
                                    p->should_stop, p->stop_context, &p->underruns);
    pthread_mutex_unlock(&p->write_mutex);
    if (result < 0 && result != -ECANCELED)
        fprintf(stderr, "[playback] direct write: %s\n", snd_strerror(result));
    return result;
}

unsigned mozart_playback_period_frames(const mozart_playback_t *p)
{
    return p ? p->period_frames : 0;
}

long mozart_playback_underruns(const mozart_playback_t *p)
{
    return p ? atomic_load(&p->underruns) : 0;
}

void mozart_playback_close(mozart_playback_t *p)
{
    if (!p) return;
    if (p->pcm) snd_pcm_close(p->pcm);
    pthread_mutex_destroy(&p->write_mutex);
    free(p);
}
