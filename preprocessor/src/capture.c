// capture.c — ALSA 采集（48 kHz / S16_LE / 2ch）
// ============================================================================
// 麦克风唯一支持格式：S16_LE / 2ch / 48 kHz / 等时端点 ASYNC（全速）。
// 按 period 阻塞读取；overrun (-EPIPE) 时 prepare 恢复并计数。
#define _POSIX_C_SOURCE 200809L
#include <alloca.h>
#include "mozart/capture.h"

#include <alsa/asoundlib.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define CAPTURE_MAX_CONSECUTIVE_ERRORS 50

struct mozart_capture {
    snd_pcm_t *pcm;
    unsigned   channels;
    unsigned   period_frames;
    long       overruns;
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

    snd_pcm_t *pcm = NULL;
    int dir = 0;
    int err = snd_pcm_open(&pcm, cfg->device, SND_PCM_STREAM_CAPTURE, 0);
    if (err < 0) {
        fprintf(stderr, "[capture] open(%s): %s\n",
                cfg->device, snd_strerror(err));
        free(c);
        return NULL;
    }

    snd_pcm_hw_params_t *hw;
    snd_pcm_hw_params_alloca(&hw);
    snd_pcm_hw_params_any(pcm, hw);

    err = snd_pcm_hw_params_set_access(pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED)
       || snd_pcm_hw_params_set_format(pcm, hw, SND_PCM_FORMAT_S16_LE)
       || snd_pcm_hw_params_set_channels(pcm, hw, c->channels);
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

static void msleep(int ms)
{
    struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}

int mozart_capture_read(mozart_capture_t *c, int16_t *buf)
{
    if (!c || !buf) return -1;

    snd_pcm_uframes_t frames = c->period_frames;
    int16_t *p = buf;
    int consecutive_errors = 0;

    while (frames > 0) {
        snd_pcm_sframes_t n = snd_pcm_readi(c->pcm, p, frames);
        if (n == (snd_pcm_sframes_t)frames) break;
        if (n > 0) {
            p += n * c->channels;
            frames -= (snd_pcm_uframes_t)n;
            continue;
        }
        if (n == -EAGAIN) { sched_yield(); continue; }
        if (n == -EPIPE) {           // overrun
            c->overruns++;
            if (snd_pcm_prepare(c->pcm) < 0) return -2;
            continue;
        }
        if (n == -ESTRPIPE) {        // 挂起，等待恢复
            while ((n = snd_pcm_resume(c->pcm)) == -EAGAIN)
                msleep(10);
            if (n < 0 && snd_pcm_prepare(c->pcm) < 0) return -3;
            continue;
        }
        if (++consecutive_errors > CAPTURE_MAX_CONSECUTIVE_ERRORS) {
            fprintf(stderr, "[capture] giving up: %s\n", snd_strerror((int)n));
            return -4;
        }
        msleep(1);
    }
    return 0;
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
