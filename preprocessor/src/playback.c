// playback.c — ALSA 播放（48 kHz / S16_LE / 2ch）
// ============================================================================
// 接收后端输出的 48 kHz / f32 / 960 样本单声道帧，转换为 S16_LE 立体声
// （L=R=mono）后写入 ALSA。按 period 阻塞写；xrun (-EPIPE) 时 prepare 恢复并
// 计数。与 capture.c 对称。
#define _POSIX_C_SOURCE 200809L
#include "mozart/playback.h"
#include "mozart/frame_meta.h"

#include <alloca.h>
#include <alsa/asoundlib.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define PLAYBACK_MAX_CONSECUTIVE_ERRORS 50

struct mozart_playback {
    snd_pcm_t *pcm;
    unsigned   channels;
    unsigned   period_frames;
    long       underruns;
};

mozart_playback_t *mozart_playback_open(const mozart_playback_config_t *cfg)
{
    if (!cfg || !cfg->device) return NULL;

    mozart_playback_t *p = calloc(1, sizeof(*p));
    if (!p) return NULL;
    p->channels      = cfg->channels ? cfg->channels : 2;
    p->period_frames = cfg->period_frames ? cfg->period_frames : 960;

    snd_pcm_t *pcm = NULL;
    int dir = 0;
    int err = snd_pcm_open(&pcm, cfg->device, SND_PCM_STREAM_PLAYBACK, 0);
    if (err < 0) {
        fprintf(stderr, "[playback] open(%s): %s\n",
                cfg->device, snd_strerror(err));
        free(p);
        return NULL;
    }

    snd_pcm_hw_params_t *hw;
    snd_pcm_hw_params_alloca(&hw);
    snd_pcm_hw_params_any(pcm, hw);

    err = snd_pcm_hw_params_set_access(pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED)
       || snd_pcm_hw_params_set_format(pcm, hw, SND_PCM_FORMAT_S16_LE)
       || snd_pcm_hw_params_set_channels(pcm, hw, p->channels);
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
        snd_pcm_uframes_t buffer = period * (cfg->n_periods ? cfg->n_periods : 4);
        err = snd_pcm_hw_params_set_period_size_near(pcm, hw, &period, &dir);
        if (err == 0)
            err = snd_pcm_hw_params_set_buffer_size_near(pcm, hw, &buffer);
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

static void msleep(int ms)
{
    struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
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
    if (!p || !pcm || nframes != p->period_frames) return -1;

    int16_t buf[MOZART_OUTPUT_SAMPLES * 2];
    float_to_s16_stereo(pcm, buf, nframes);

    snd_pcm_uframes_t frames = nframes;
    int16_t *ptr = buf;
    int consecutive_errors = 0;

    while (frames > 0) {
        snd_pcm_sframes_t n = snd_pcm_writei(p->pcm, ptr, frames);
        if (n == (snd_pcm_sframes_t)frames) break;
        if (n > 0) {
            ptr += n * p->channels;
            frames -= (snd_pcm_uframes_t)n;
            continue;
        }
        if (n == -EAGAIN) { sched_yield(); continue; }
        if (n == -EPIPE) {           // underrun
            p->underruns++;
            if (snd_pcm_prepare(p->pcm) < 0) return -2;
            continue;
        }
        if (n == -ESTRPIPE) {        // 挂起，等待恢复
            while ((n = snd_pcm_resume(p->pcm)) == -EAGAIN)
                msleep(10);
            if (n < 0 && snd_pcm_prepare(p->pcm) < 0) return -3;
            continue;
        }
        if (++consecutive_errors > PLAYBACK_MAX_CONSECUTIVE_ERRORS) {
            fprintf(stderr, "[playback] giving up: %s\n", snd_strerror((int)n));
            return -4;
        }
        msleep(1);
    }
    return 0;
}

long mozart_playback_underruns(const mozart_playback_t *p)
{
    return p ? p->underruns : 0;
}

void mozart_playback_close(mozart_playback_t *p)
{
    if (!p) return;
    if (p->pcm) snd_pcm_close(p->pcm);
    free(p);
}
