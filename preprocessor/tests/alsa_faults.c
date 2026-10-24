// 通过动态链接注入实际 ALSA 调用错误，生产代码不包含故障开关。
#define _GNU_SOURCE
#include <alsa/asoundlib.h>
#include <dlfcn.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static snd_pcm_sframes_t transfer(snd_pcm_t *pcm, void *buffer,
                                  snd_pcm_uframes_t frames, int capture)
{
    static unsigned read_calls, write_calls;
    unsigned *calls = capture ? &read_calls : &write_calls;
    const char *mode = getenv(capture ? "MOZART_FAULT_READ" : "MOZART_FAULT_WRITE");
    ++*calls;
    if (mode && *calls == 1) {
        const char *message = capture ? "[fault] read\n" : "[fault] write\n";
        write(STDERR_FILENO, message, strlen(message));
    }
    if (mode) {
        if (!strcmp(mode, "device_gone")) return -ENODEV;
        if (!strcmp(mode, "cancelled")) return -ECANCELED;
        if (!strcmp(mode, "again")) return -EAGAIN;
        if (!strcmp(mode, "after_first_again") && *calls > 1) return -EAGAIN;
        if (!strcmp(mode, "after_second_again") && *calls > 2) return -EAGAIN;
        if (!strcmp(mode, "suspend")) return -ESTRPIPE;
        if (!strcmp(mode, "xrun")) return -EPIPE;
        if (!strcmp(mode, "recover_suspend") && *calls == 1) return -ESTRPIPE;
        if (!strcmp(mode, "recover_xrun") && *calls == 1) return -EPIPE;
        if (!strcmp(mode, "partial") && *calls == 1) frames /= 2;
    }
    if (capture) {
        snd_pcm_sframes_t (*real_read)(snd_pcm_t *, void *, snd_pcm_uframes_t);
        *(void **)(&real_read) = dlsym(RTLD_NEXT, "snd_pcm_readi");
        return real_read(pcm, buffer, frames);
    }
    snd_pcm_sframes_t (*real_write)(snd_pcm_t *, const void *, snd_pcm_uframes_t);
    *(void **)(&real_write) = dlsym(RTLD_NEXT, "snd_pcm_writei");
    return real_write(pcm, buffer, frames);
}

snd_pcm_sframes_t snd_pcm_readi(snd_pcm_t *pcm, void *buffer, snd_pcm_uframes_t frames)
{
    return transfer(pcm, buffer, frames, 1);
}

snd_pcm_sframes_t snd_pcm_writei(snd_pcm_t *pcm, const void *buffer, snd_pcm_uframes_t frames)
{
    return transfer(pcm, (void *)buffer, frames, 0);
}

int snd_pcm_resume(snd_pcm_t *pcm)
{
    (void)pcm;
    static unsigned calls;
    ++calls;
    if (calls == 1) write(STDERR_FILENO, "[fault] resume\n", 15);
    const char *read_mode = getenv("MOZART_FAULT_READ");
    const char *write_mode = getenv("MOZART_FAULT_WRITE");
    if ((read_mode && !strcmp(read_mode, "suspend"))
        || (write_mode && !strcmp(write_mode, "suspend"))) return -EAGAIN;
    return calls < 3 ? -EAGAIN : 0;
}

int snd_pcm_open(snd_pcm_t **pcm, const char *name, snd_pcm_stream_t stream, int mode)
{
    if ((getenv("MOZART_FAULT_READ") || getenv("MOZART_FAULT_WRITE"))
        && !(mode & SND_PCM_NONBLOCK)) {
        const char message[] = "[fault] blocking_open\n";
        write(STDERR_FILENO, message, sizeof(message)-1);
    }
    int (*real_open)(snd_pcm_t **, const char *, snd_pcm_stream_t, int);
    *(void **)(&real_open) = dlsym(RTLD_NEXT, "snd_pcm_open");
    return real_open(pcm, name, stream, mode);
}
