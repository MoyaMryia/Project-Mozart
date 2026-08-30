// wav.c — 离线 WAV 输入（PCM 16bit）
#include "mozart/wav.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define WAV_FMT_PCM 1

struct mozart_wav_reader {
    FILE   *f;
    long    data_start;
    long    data_bytes;
    long    data_pos;
    unsigned rate;
    unsigned channels;
};

static unsigned read_u16(FILE *f)  { unsigned char b[2]; fread(b,1,2,f); return b[0] | (b[1]<<8); }
static unsigned read_u32(FILE *f)  { unsigned char b[4]; fread(b,1,4,f);
                                     return b[0] | (b[1]<<8) | (b[2]<<16) | ((unsigned)b[3]<<24); }

mozart_wav_reader_t *mozart_wav_open(const char *path)
{
    FILE *f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "[wav] cannot open %s\n", path); return NULL; }

    unsigned char hdr[12];
    if (fread(hdr, 1, 12, f) != 12 || memcmp(hdr, "RIFF", 4) || memcmp(hdr+8, "WAVE", 4)) {
        fprintf(stderr, "[wav] %s: not RIFF/WAVE\n", path);
        fclose(f);
        return NULL;
    }

    unsigned format = 0, channels = 0, rate = 0, bits = 0;
    long data_start = -1, data_bytes = 0;

    for (;;) {
        unsigned char ch[8];
        if (fread(ch, 1, 8, f) != 8) break;
        unsigned size = ch[4] | (ch[5]<<8) | (ch[6]<<16) | ((unsigned)ch[7]<<24);
        long next = ftell(f) + size + (size & 1); // RIFF 块 2 字节对齐

        if (!memcmp(ch, "fmt ", 4)) {
            format  = read_u16(f);
            channels = read_u16(f);
            rate    = read_u32(f);
            fseek(f, 6, SEEK_CUR);   // byterate(4) + blockalign(2)
            bits     = read_u16(f);
        } else if (!memcmp(ch, "data", 4)) {
            data_start = ftell(f);
            data_bytes = size;
        }
        fseek(f, next, SEEK_SET);
        if (data_start >= 0 && format) break;
    }

    if (format != WAV_FMT_PCM || bits != 16) {
        fprintf(stderr, "[wav] %s: need PCM 16bit (got fmt=%u bits=%u)\n",
                path, format, bits);
        fclose(f);
        return NULL;
    }
    if (channels != 2 || rate != 48000) {
        fprintf(stderr, "[wav] %s: need 48 kHz stereo (got %u Hz / %u ch)\n",
                path, rate, channels);
        fclose(f);
        return NULL;
    }
    if (data_start < 0 || data_bytes == 0) {
        fprintf(stderr, "[wav] %s: no data chunk\n", path);
        fclose(f);
        return NULL;
    }

    mozart_wav_reader_t *r = calloc(1, sizeof(*r));
    r->f = f;
    r->data_start = data_start;
    r->data_bytes = data_bytes;
    r->rate = rate;
    r->channels = channels;
    fseek(f, data_start, SEEK_SET);
    return r;
}

int mozart_wav_read(mozart_wav_reader_t *r, int16_t *buf, int frames)
{
    if (!r || frames <= 0) return 0;
    long remain = r->data_bytes - r->data_pos;
    size_t want = (size_t)frames * r->channels * sizeof(int16_t);
    if ((long)want > remain) want = (size_t)remain;
    size_t got = fread(buf, 1, want, r->f);
    if (got == 0) return 0;
    r->data_pos += (long)got;
    return (int)(got / (r->channels * sizeof(int16_t)));
}

void mozart_wav_rewind(mozart_wav_reader_t *r)
{
    if (!r) return;
    r->data_pos = 0;
    fseek(r->f, r->data_start, SEEK_SET);
}

void mozart_wav_close(mozart_wav_reader_t *r)
{
    if (!r) return;
    fclose(r->f);
    free(r);
}
