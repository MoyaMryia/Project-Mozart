#define _POSIX_C_SOURCE 200809L
#include "mozart.h"
#include "mozart/pipeline.h"
#include "mozart/rnnoise.h"
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

#define INPUT_FILE  "noisy_sample.wav"
#define OUTPUT_FILE "output.wav"

#define FRAME_SAMPLES 480   // RNNoise native: 48 kHz × 10 ms
#define SAMPLE_RATE   48000

static inline float clampf(float x, float lo, float hi)
{
    return x < lo ? lo : (x > hi ? hi : x);
}

static void write_wav_header(FILE *f, int sr, int ch, int *dso)
{
    uint32_t br = sr * ch * sizeof(int16_t);
    uint16_t ba = ch * sizeof(int16_t);
    uint16_t bps = 16;

    uint32_t cs = 36; fwrite("RIFF", 1, 4, f); fwrite(&cs, 4, 1, f);
    fwrite("WAVE", 1, 4, f);
    fwrite("fmt ", 1, 4, f);
    uint32_t fs = 16; uint16_t af = 1;
    fwrite(&fs, 4, 1, f); fwrite(&af, 2, 1, f);
    fwrite(&ch, 2, 1, f);  fwrite(&sr, 4, 1, f);
    fwrite(&br, 4, 1, f);  fwrite(&ba, 2, 1, f);
    fwrite(&bps, 2, 1, f);
    fwrite("data", 1, 4, f);
    *dso = (int)ftell(f);
    uint32_t ds = 0; fwrite(&ds, 4, 1, f);
}

static void finalise_wav_header(FILE *f, int dso, int ns)
{
    int ds = ns * sizeof(int16_t);
    int fs = dso + ds;
    fseek(f, 4,  SEEK_SET); fwrite(&fs, 4, 1, f);
    fseek(f, dso, SEEK_SET); fwrite(&ds, 4, 1, f);
    fseek(f, 0, SEEK_END);
}

int main(void)
{
    setbuf(stdout, NULL);
    printf("Mozart pre-processing v%s (RNNoise @ %d Hz / %d samples)\n",
           mozart_pre_version(), SAMPLE_RATE, FRAME_SAMPLES);

    char cmd[512];
    snprintf(cmd, sizeof(cmd),
             "ffmpeg -i \"%s\" -f f32le -acodec pcm_f32le -ar %d -ac %d "
             "-hide_banner -loglevel error -",
             INPUT_FILE, SAMPLE_RATE, MOZART_CHANNELS);

    FILE *ffmpeg = popen(cmd, "r");
    if (!ffmpeg) { fprintf(stderr, "ERROR: ffmpeg failed\n"); return 1; }

    FILE *fout = fopen(OUTPUT_FILE, "wb");
    if (!fout) { fprintf(stderr, "ERROR: cannot open %s\n", OUTPUT_FILE); pclose(ffmpeg); return 1; }

    int dso;
    write_wav_header(fout, SAMPLE_RATE, MOZART_CHANNELS, &dso);

    mozart_pipeline_t *pl = mozart_pipeline_new();
    if (!pl) { fprintf(stderr, "ERROR: pipeline new\n"); pclose(ffmpeg); fclose(fout); return 1; }

    mozart_stage_t *rn = mozart_rnnoise_new(NULL);
    if (!rn) { fprintf(stderr, "ERROR: rnnoise new\n"); mozart_pipeline_destroy(pl); pclose(ffmpeg); fclose(fout); return 1; }
    mozart_pipeline_add_stage(pl, rn);

    float in[FRAME_SAMPLES];
    float out[FRAME_SAMPLES];
    mozart_frame_meta_t meta = {0};
    int fc = 0, ts = 0;

    for (;;) {
        size_t n = fread(in, sizeof(float), FRAME_SAMPLES, ffmpeg);
        if (n == 0) break;
        for (size_t i = n; i < FRAME_SAMPLES; i++) in[i] = 0.0f;

        meta.frame_idx = fc + 1;
        int rc = mozart_pipeline_process(pl, in, FRAME_SAMPLES, out, &meta);
        if (rc < 0) { fprintf(stderr, "frame %d error %d\n", fc, rc); break; }

        for (int i = 0; i < FRAME_SAMPLES; i++) {
            float s = clampf(out[i], -1.0f, 1.0f);
            int16_t pcm = (int16_t)(s * 32767.0f);
            fwrite(&pcm, sizeof(pcm), 1, fout);
        }
        ts += FRAME_SAMPLES;
        fc++;
    }

    finalise_wav_header(fout, dso, ts);
    fclose(fout);
    pclose(ffmpeg);
    mozart_pipeline_destroy(pl);

    printf("Done: %d frames (%.2f s) -> %s\n", fc, (double)ts / SAMPLE_RATE, OUTPUT_FILE);
    return 0;
}
