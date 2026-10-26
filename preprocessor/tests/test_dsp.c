// test_dsp.c — DSP 链无框架单测（手写断言）
// ============================================================================
// 覆盖：立体声取 FL、高通对直流的衰减、降采样增益、契约帧元数据、
//       分段滞回基本行为。VAD 来源 = WOLA 后验 SNR 置信度（单干 M1）。
#include "mozart/dsp.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MOZART_PI 3.14159265358979323846f
#define MIN_NOISE_SUPPRESSION_DB 10.0

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
// 注意：关闭 WOLA——纯稳态正弦与哼声同类，会被谱降噪正常压制。
static void test_passband_gain(void)
{
    printf("test_passband_gain\n");
    mozart_dsp_config_t cfg = { .denoise = false };
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

static void test_snr_vad_hangover(void)
{
    printf("test_energy_vad_hangover\n");
    // SNR-VAD：稳态噪声与语音同类；静音后接宽带爆发才能形成高后验 SNR。
    mozart_dsp_t *d = mozart_dsp_new(NULL);
    mozart_input_frame_t out;
    int16_t in[MOZART_RAW_SAMPLES * 2];
    unsigned lcg = 12345;
    memset(in, 0, sizeof(in));
    for (int k = 0; k < 25; ++k) mozart_dsp_process(d, in, &out);  // 0.5 s 静音打底
    for (int k = 0; k < 6; ++k) {
        for (int i = 0; i < MOZART_RAW_SAMPLES; ++i) {
            lcg = lcg * 1103515245u + 12345u;
            const int16_t sample = (int16_t)((int)(lcg >> 16 & 0xffff) - 32768) / 3;
            in[2*i] = in[2*i+1] = sample;
        }
        mozart_dsp_process(d, in, &out);
    }
    uint8_t segment = out.meta.segment_id;
    CHECK(segment != 0, "SNR VAD enters speech after broadband bursts");
    memset(in, 0, sizeof(in));
    for (int k = 0; k < 10; ++k) mozart_dsp_process(d, in, &out);
    CHECK(out.meta.segment_id == segment, "200ms pause preserves utterance");
    for (int k = 0; k < 50; ++k) mozart_dsp_process(d, in, &out);
    CHECK(out.meta.segment_id == 0, "sustained silence ends utterance");
    mozart_dsp_free(d);
}

// P0 回归：生产路径（dsp 层）对稳态白噪必须实际降噪——曾因初始化缺陷增益恒 1。
static void test_production_path_suppresses_noise(void)
{
    printf("test_production_path_suppresses_noise\n");
    mozart_dsp_t *d = mozart_dsp_new(&(mozart_dsp_config_t){ .denoise = true });
    CHECK(d != NULL, "dsp_new denoise");
    unsigned lcg = 42;
    float in[MOZART_RAW_SAMPLES];
    float out[MOZART_RAW_SAMPLES * 3];
    double in_sq = 0, out_sq = 0;
    long counted = 0;
    for (int block = 0; block < 100; block++) {
        for (int i = 0; i < MOZART_RAW_SAMPLES; i++) {
            lcg = lcg * 1103515245u + 12345u;
            in[i] = (float)((int)(lcg >> 16 & 0xffffu) - 32768) / 32768.0f * 0.3f;
        }
        const size_t n = mozart_dsp_enhance(d, in, MOZART_RAW_SAMPLES, out,
                                            sizeof(out) / sizeof(out[0]));
        CHECK(n != (size_t)-1, "enhance");
        if (block < 20) continue;   // 跳过收敛期
        for (int i = 0; i < MOZART_RAW_SAMPLES; i++) in_sq += (double)in[i] * in[i];
        const size_t use = n < MOZART_RAW_SAMPLES ? n : MOZART_RAW_SAMPLES;
        for (size_t i = 0; i < use; i++) out_sq += (double)out[i] * out[i];
        counted += (long)use;
    }
    const double suppression = 10.0 * log10((in_sq / 76800.0) / (out_sq / (double)counted + 1e-20));
    printf("  suppression=%.1f dB\n", suppression);
    CHECK(suppression > MIN_NOISE_SUPPRESSION_DB, "production path suppresses white noise > 10 dB");

    // 同一流先切旁路，再恢复降噪；分别对输入功率验收，排除仅改遥测的实现。
    for (int phase = 0; phase < 2; phase++) {
        mozart_dsp_set_stages(d, phase != 0, false, false, false);
        double input_power = 0.0, output_power = 0.0;
        for (int block = 0; block < 100; block++) {
            for (int i = 0; i < MOZART_RAW_SAMPLES; i++) {
                lcg = lcg * 1103515245u + 12345u;
                in[i] = (float)((int)(lcg >> 16 & 0xffffu) - 32768) / 32768.0f * 0.3f;
            }
            const size_t n = mozart_dsp_enhance(d, in, MOZART_RAW_SAMPLES, out,
                                                sizeof(out) / sizeof(out[0]));
            CHECK(n == MOZART_RAW_SAMPLES, "runtime switch keeps output count");
            if (block < 20) continue;
            for (int i = 0; i < MOZART_RAW_SAMPLES; i++) {
                input_power += (double)in[i] * in[i];
                output_power += (double)out[i] * out[i];
            }
        }
        if (phase == 0) {
            // 白噪旁路只经过高通；功率容差覆盖滤波衰减及统计波动。
            const double ratio = output_power / input_power;
            CHECK(ratio > 0.8 && ratio < 1.2, "runtime bypass preserves input noise power");
        } else {
            CHECK(10.0 * log10(input_power / (output_power + 1e-20)) > MIN_NOISE_SUPPRESSION_DB,
                  "runtime denoise restores suppression > 10 dB");
        }
    }

    // 旧会话有噪声估计。重置后用静音预热，再用低电平噪声检查估计器已清空。
    mozart_dsp_reset(d);
    memset(in, 0, sizeof(in));
    size_t n = mozart_dsp_enhance(d, in, MOZART_RAW_SAMPLES, out,
                                  sizeof(out) / sizeof(out[0]));
    CHECK(n == MOZART_RAW_SAMPLES - 64, "reset restores WOLA startup emission count");
    float silence_peak = 0.0f;
    for (int i = 0; i < MOZART_RAW_SAMPLES - 64; i++)
        silence_peak = fmaxf(silence_peak, fabsf(out[i]));
    CHECK(silence_peak == 0.0f, "reset clears old waveform history");
    double burst_input = 0.0, burst_output = 0.0;
    for (int i = 0; i < MOZART_RAW_SAMPLES; i++) {
        lcg = lcg * 1103515245u + 12345u;
        in[i] = (float)((int)(lcg >> 16 & 0xffffu) - 32768) / 32768.0f * 0.1f;
        burst_input += (double)in[i] * in[i];
    }
    n = mozart_dsp_enhance(d, in, MOZART_RAW_SAMPLES, out,
                           sizeof(out) / sizeof(out[0]));
    CHECK(n == MOZART_RAW_SAMPLES, "reset stream reaches steady emission count");
    for (int i = 0; i < MOZART_RAW_SAMPLES; i++)
        burst_output += (double)out[i] * out[i];
    // 静音基线后的突发应通过；0.5 功率比覆盖末窗延迟和高通衰减。
    CHECK(burst_output / burst_input > 0.5, "reset discards previous noise estimate");
    mozart_dsp_free(d);
}

// 每个 64 样本块都切换降噪；启动后每块必须连续产出 64 样本。
static void test_stage_toggle_preserves_output_count(void)
{
    printf("test_stage_toggle_preserves_output_count\n");
    mozart_dsp_t *d = mozart_dsp_new(NULL);
    float in[64] = {0}, out[1024];
    size_t total = 0;
    for (int k = 1; k <= 80; k++) {
        mozart_dsp_set_stages(d, (k % 2) != 0, false, false, false);
        const size_t n = mozart_dsp_enhance(d, in, 64, out, 1024);
        const size_t expected = k < 9 ? 0 : (k == 9 ? 512 : 64);
        CHECK(n == expected, "stage toggle preserves WOLA emission count");
        total += n;
    }
    CHECK(total == 80 * 64 - 64, "only the final overlap remains pending");
    mozart_dsp_telemetry_t tm;
    mozart_dsp_telemetry(d, &tm);
    CHECK(tm.denoise == 0, "denoise switch is applied");
    mozart_dsp_free(d);
}

int main(void)
{
    test_channel_pick();
    test_right_channel_ignored();
    test_passband_gain();
    test_meta_and_vad();
    test_reset();
    test_snr_vad_hangover();
    test_production_path_suppresses_noise();
    test_stage_toggle_preserves_output_count();
    if (g_fail) {
        printf("\n%d test(s) FAILED\n", g_fail);
        return 1;
    }
    printf("\nall tests passed\n");
    return 0;
}
