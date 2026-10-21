// capture.c — ALSA 采集（48 kHz / S16_LE / 2ch）
// ============================================================================
// 麦克风唯一支持格式：S16_LE / 2ch / 48 kHz / 等时端点 ASYNC（全速）。
// 按 period 读取；非阻塞 I/O 支持停止请求，overrun 时按有期限策略恢复。
#define _POSIX_C_SOURCE 200809L
#include <alloca.h>
#include "mozart/capture.h"

#include <alsa/asoundlib.h>
#include "pcm_io.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

struct mozart_capture {
    snd_pcm_t *pcm;
    unsigned   channels;
    unsigned   period_frames;
    long       overruns;
    bool     (*should_stop)(void *);
    void      *stop_context;
};

uint64_t mozart_now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

mozart_capture_t *mozart_capture_open(const mozart_capture_config_t *cfg)
{
    if (!cfg || !cfg->device) return NULL;

    mozart_capture_t *c = calloc(1, sizeof(*c));
    if (!c) return NULL;
    c->channels      = cfg->channels ? cfg->channels : 2;
    c->period_frames = cfg->period_frames ? cfg->period_frames : 960;
    c->should_stop = cfg->should_stop;
    c->stop_context = cfg->stop_context;

    snd_pcm_t *pcm = NULL;
    int dir = 0;
    int err = snd_pcm_open(&pcm, cfg->device, SND_PCM_STREAM_CAPTURE, SND_PCM_NONBLOCK);
    if (err < 0) {
        fprintf(stderr, "[capture] open(%s): %s\n",
                cfg->device, snd_strerror(err));
        free(c);
        return NULL;
    }

    snd_pcm_hw_params_t *hw;
    snd_pcm_hw_params_alloca(&hw);
    snd_pcm_hw_params_any(pcm, hw);

    err = snd_pcm_hw_params_set_access(pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED);
    if (err >= 0) err = snd_pcm_hw_params_set_format(pcm, hw, SND_PCM_FORMAT_S16_LE);
    if (err >= 0) err = snd_pcm_hw_params_set_channels(pcm, hw, c->channels);
    if (err == 0) {
        unsigned rate = cfg->rate;
        err = snd_pcm_hw_params_set_rate_near(pcm, hw, &rate, &dir);
        if (err == 0 && rate != cfg->rate) {
            fprintf(stderr, "[capture] device gave %u Hz, want %u\n",
                    rate, cfg->rate);
            err = -1;
        }
    }
    if (err == 0) {
        snd_pcm_uframes_t period = c->period_frames;
        snd_pcm_uframes_t buffer = period * (cfg->n_periods ? cfg->n_periods : 4);
        err = snd_pcm_hw_params_set_period_size_near(pcm, hw, &period, &dir);
        if (err == 0)
            err = snd_pcm_hw_params_set_buffer_size_near(pcm, hw, &buffer);
    }
    if (err == 0) err = snd_pcm_hw_params(pcm, hw);
    if (err < 0) {
        fprintf(stderr, "[capture] hw_params: %s\n", snd_strerror(err));
        snd_pcm_close(pcm);
        free(c);
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
        fprintf(stderr, "[capture] prepare: %s\n", snd_strerror(err));
        snd_pcm_close(pcm);
        free(c);
        return NULL;
    }

    c->pcm = pcm;
    return c;
}


int mozart_capture_read(mozart_capture_t *c, int16_t *buf)
{
    if (!c || !buf) return -1;

    const int result = pcm_transfer(c->pcm, buf, c->period_frames, c->channels,
                                    true, c->should_stop, c->stop_context, &c->overruns);
    if (result < 0 && result != -ECANCELED)
        fprintf(stderr, "[capture] read: %s\n", snd_strerror(result));
    return result;
}

void mozart_capture_close(mozart_capture_t *c)
{
    if (!c) return;
    if (c->pcm) snd_pcm_close(c->pcm);
    free(c);
}

long mozart_capture_overruns(const mozart_capture_t *c)
{
    return c ? c->overruns : 0;
}
