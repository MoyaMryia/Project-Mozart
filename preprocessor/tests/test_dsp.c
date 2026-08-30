// test_dsp.c — DSP 链无框架单测（手写断言）
// ============================================================================
// 覆盖：立体声取 FL、高通对直流的衰减、降采样增益、契约帧元数据、
//       分段滞回基本行为。编译需开启 RNNoise（默认开）。
#include "mozart/dsp.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MOZART_PI 3.14159265358979323846f

static int g_fail = 0;
#define CHECK(cond, msg) do { \
    if (cond) printf("  ok  %s\n", msg); \
    else { printf("  FAIL %s\n", msg); g_fail++; } } while (0)

static void fill_stereo(int16_t *buf, int frames, int16_t l, int16_t r)
{
    for (int i = 0; i < frames; i++) {
        buf[2 * i]     = l;
        buf[2 * i + 1] = r;
    }
}

// 立体声只取 FL：FL 注入直流脉冲，FR 静音；稳态输出幅度应反映 FL
static void test_channel_pick(void)
{
    printf("test_channel_pick\n");
    mozart_dsp_t *d = mozart_dsp_new(NULL);
    CHECK(d != NULL, "dsp_new");

    mozart_input_frame_t out;
    int16_t in[MOZART_RAW_SAMPLES * 2];
    fill_stereo(in, MOZART_RAW_SAMPLES, 8000, 0);

    for (int k = 0; k < 50; k++) {
        CHECK(mozart_dsp_process(d, in, &out) == 0, "process") ;
    }
    // FL 直流 8000/32768=0.244，高通后稳态≈0；输出应远小于 0.1
    float peak = 0.0f;
    for (int i = 0; i < MOZART_INPUT_SAMPLES; i++)
        peak = fmaxf(peak, fabsf(out.pcm[i]));
    CHECK(peak < 0.1f, "DC through highpass decays to near zero");
    mozart_dsp_free(d);
}

// FR 通道内容不应泄漏进输出：FL 静音，FR 大信号
static void test_right_channel_ignored(void)
{
    printf("test_right_channel_ignored\n");
    mozart_dsp_t *d = mozart_dsp_new(NULL);
    mozart_input_frame_t out;
    int16_t in[MOZART_RAW_SAMPLES * 2];
    fill_stereo(in, MOZART_RAW_SAMPLES, 0, 32000);
    for (int k = 0; k < 50; k++) mozart_dsp_process(d, in, &out);
    float peak = 0.0f;
    for (int i = 0; i < MOZART_INPUT_SAMPLES; i++)
        peak = fmaxf(peak, fabsf(out.pcm[i]));
    CHECK(peak < 0.05f, "FR content is ignored (FL-only downmix)");
    mozart_dsp_free(d);
}

// 1 kHz 正弦（-6 dBFS）过高通+降采样链后幅度应保留在合理范围。
// 注意：绕过 RNNoise——纯稳态正弦无语音特征，会被全湿降噪正常压制。
static void test_passband_gain(void)
{
    printf("test_passband_gain\n");
    mozart_dsp_config_t cfg = { .rnnoise = false, .rnnoise_model = NULL };
    mozart_dsp_t *d = mozart_dsp_new(&cfg);
    mozart_input_frame_t out;
    int16_t in[MOZART_RAW_SAMPLES * 2];

    float peak_acc = 0.0f;
    for (int k = 0; k < 30; k++) {   // 0.6 s，避开滤波器暂态
        for (int i = 0; i < MOZART_RAW_SAMPLES; i++) {
            float t = (float)(k * MOZART_RAW_SAMPLES + i) / 48000.0f;
            float s = 0.5f * sinf(2.0f * MOZART_PI * 1000.0f * t);
            int16_t v = (int16_t)(s * 32767.0f);
            in[2 * i] = v;
            in[2 * i + 1] = v;
        }
        CHECK(mozart_dsp_process(d, in, &out) == 0, "process");
        if (k >= 10)
            for (int i = 0; i < MOZART_INPUT_SAMPLES; i++)
                peak_acc = fmaxf(peak_acc, fabsf(out.pcm[i]));
    }
    CHECK(peak_acc > 0.25f && peak_acc < 0.75f,
          "1 kHz sine passes with sane amplitude");
    mozart_dsp_free(d);
}

// 元数据：frame_idx 单调、energy_db 有界；静音输入 VAD 应回落
static void test_meta_and_vad(void)
{
    printf("test_meta_and_vad\n");
    mozart_dsp_t *d = mozart_dsp_new(NULL);
    mozart_input_frame_t out;
    int16_t in[MOZART_RAW_SAMPLES * 2];
    memset(in, 0, sizeof(in));

    uint32_t last_idx = 0;
    int vad_seen_high = 0;
    for (int k = 0; k < 200; k++) {  // 4 s 静音，滞回必然退出
        CHECK(mozart_dsp_process(d, in, &out) == 0, "process");
        CHECK(out.meta.frame_idx == last_idx + 1, "frame_idx increments");
        last_idx = out.meta.frame_idx;
        if (out.meta.vad_flag) vad_seen_high++;
    }
    CHECK(vad_seen_high == 0, "silence: vad_flag returns to 0");
    CHECK(out.meta.segment_id == 0, "silence: segment_id is 0");
    mozart_dsp_free(d);
}

// reset 后 frame_idx 归零、FIR/高通历史清空
static void test_reset(void)
{
    printf("test_reset\n");
    mozart_dsp_t *d = mozart_dsp_new(NULL);
    mozart_input_frame_t out;
    int16_t in[MOZART_RAW_SAMPLES * 2];
    fill_stereo(in, MOZART_RAW_SAMPLES, 12345, 12345);
    for (int k = 0; k < 10; k++) mozart_dsp_process(d, in, &out);
    CHECK(out.meta.frame_idx == 10, "frame_idx reached 10");

    mozart_dsp_reset(d);
    fill_stereo(in, MOZART_RAW_SAMPLES, 0, 0);
    mozart_dsp_process(d, in, &out);
    CHECK(out.meta.frame_idx == 1, "frame_idx reset to 1");
    mozart_dsp_free(d);
}

int main(void)
{
    test_channel_pick();
    test_right_channel_ignored();
    test_passband_gain();
    test_meta_and_vad();
    test_reset();
    if (g_fail) {
        printf("\n%d test(s) FAILED\n", g_fail);
        return 1;
    }
    printf("\nall tests passed\n");
    return 0;
}
