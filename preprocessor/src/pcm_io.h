// ALSA 非阻塞传输与有期限恢复。采集和播放共用等待策略。
#ifndef MOZART_PCM_IO_H
#define MOZART_PCM_IO_H

#include <alsa/asoundlib.h>
#include <stdbool.h>
#include <stdint.h>
#include <time.h>

#define MOZART_PCM_RETRY_NS 2000000L
#define MOZART_PCM_NO_PROGRESS_NS 1000000000ULL

static inline uint64_t pcm_now_ns(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (uint64_t)now.tv_sec * 1000000000ULL + (uint64_t)now.tv_nsec;
}

static inline int pcm_transfer(snd_pcm_t *pcm, int16_t *buffer,
                               snd_pcm_uframes_t frames, unsigned channels,
                               bool capture, bool (*should_stop)(void *),
                               void *stop_context, long *xruns)
{
    uint64_t deadline = pcm_now_ns() + MOZART_PCM_NO_PROGRESS_NS;
    bool suspended = false;
    while (frames > 0) {
        if (should_stop && should_stop(stop_context)) return -ECANCELED;
        if (pcm_now_ns() >= deadline) return -ETIMEDOUT;
        snd_pcm_sframes_t n;
        if (suspended) {
            n = snd_pcm_resume(pcm);
            if (n != -EAGAIN) {
                if (n < 0) n = snd_pcm_prepare(pcm);
                if (n < 0) return (int)n;
                suspended = false;
            }
        } else {
            n = capture ? snd_pcm_readi(pcm, buffer, frames)
                        : snd_pcm_writei(pcm, buffer, frames);
            if (n > 0) {
                buffer += n * channels;
                frames -= (snd_pcm_uframes_t)n;
                deadline = pcm_now_ns() + MOZART_PCM_NO_PROGRESS_NS;
                continue;
            }
            if (n == -EPIPE) {
                ++*xruns;
                n = snd_pcm_prepare(pcm);
                if (n < 0) return (int)n;
            } else if (n == -ESTRPIPE) {
                suspended = true;
            } else if (n != 0 && n != -EAGAIN && n != -EINTR) {
                return (int)n;
            }
        }
        const struct timespec retry = { .tv_sec = 0, .tv_nsec = MOZART_PCM_RETRY_NS };
        nanosleep(&retry, NULL);
    }
    return 0;
}

#endif
