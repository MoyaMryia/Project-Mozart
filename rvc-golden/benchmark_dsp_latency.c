/* 测量现有数字信号处理函数。文件读取不计入帧处理时间。 */
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include "mozart/dsp.h"
#include "mozart/wav.h"

static double now_ms(void) {
    struct timespec value;
    clock_gettime(CLOCK_MONOTONIC, &value);
    return value.tv_sec * 1000.0 + value.tv_nsec / 1e6;
}

int main(int argc, char **argv) {
    if (argc != 3) return 2;
    printf("{\"records\":[");
    for (int enabled = 0; enabled < 2; ++enabled) {
        mozart_dsp_config_t config = { .rnnoise = enabled, .rnnoise_model = argv[2] };
        double began = now_ms();
        mozart_dsp_t *dsp = mozart_dsp_new(&config);
        double startup = now_ms() - began;
        mozart_wav_reader_t *wav = mozart_wav_open(argv[1]);
        if (!dsp || !wav) return 3;
        int16_t stereo[960 * 2];
        mozart_input_frame_t output;
        memset(&output, 0, sizeof(output));
        for (int frame = 0; frame < 50; ++frame) {
            memset(stereo, 0, sizeof(stereo));
            if (!mozart_wav_read(wav, stereo, 960)) mozart_wav_rewind(wav);
            if (mozart_dsp_process(dsp, stereo, &output)) return 4;
        }
        printf("%s{\"rnnoise\":%s,\"constructor_ms\":%.6f,\"frame_ms\":[",
               enabled ? "," : "", enabled ? "true" : "false", startup);
        int count = 0;
        for (int repeat = 0; repeat < 3; ++repeat) {
            mozart_wav_rewind(wav);
            mozart_dsp_reset(dsp);
            for (;;) {
                memset(stereo, 0, sizeof(stereo));
                if (!mozart_wav_read(wav, stereo, 960)) break;
                began = now_ms();
                if (mozart_dsp_process(dsp, stereo, &output)) return 4;
                double elapsed = now_ms() - began;
                printf("%s%.6f", count++ ? "," : "", elapsed);
            }
        }
        printf("]}");
        mozart_wav_close(wav);
        mozart_dsp_free(dsp);
    }
    printf("]}\n");
    return 0;
}
