// dsp.c — 48 kHz 单干增强 + 16 kHz 契约帧打包
// ============================================================================
// 见 dsp.h 顶部说明。有状态组件（高通 biquad、WOLA 降噪器、整形级、
// 3:1 FIR 历史、VAD 滞回、frame_idx）集中在本模块，mozart_dsp_reset 一键清空。
#include "mozart/dsp.h"
#include "mozart/noise_suppressor.h"
#include "mozart/stages.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#define DECIMATOR_TAPS 33

// 80 Hz 二阶高通（butterworth, f0=80Hz @48k）
#define HP_B0  0.99262254f
#define HP_B1 -1.98524509f
#define HP_B2  0.99262254f
#define HP_A1 -1.98519066f
#define HP_A2  0.98529951f

// 7.2 kHz 低通，Kaiser 窗；抑制 16 kHz Nyquist 以上混叠
static const float decimator_coeffs[DECIMATOR_TAPS] = {
     4.2914726915e-04f,  1.4895510869e-03f,  1.5189746882e-03f,
    -1.2652814003e-03f, -5.8142304759e-03f, -7.0800492720e-03f,
     0.0000000000e+00f,  1.3328130710e-02f,  2.0914031264e-02f,
     8.9839746220e-03f, -2.2538271381e-02f, -5.0922926864e-02f,
    -4.0588192824e-02f,  3.0283637766e-02f,  1.4611043959e-01f,
     2.5519851986e-01f,  2.9990509072e-01f,  2.5519851986e-01f,
     1.4611043959e-01f,  3.0283637766e-02f, -4.0588192824e-02f,
    -5.0922926864e-02f, -2.2538271381e-02f,  8.9839746220e-03f,
     2.0914031264e-02f,  1.3328130710e-02f,  0.0000000000e+00f,
    -7.0800492720e-03f, -5.8142304759e-03f, -1.2652814003e-03f,
     1.5189746882e-03f,  1.4895510869e-03f,  4.2914726915e-04f
};

// VAD 滞回参数（20 ms/帧，来源 = WOLA 置信度）
#define SEG_ENTER_PROB   0.6f
#define SEG_ENTER_FRAMES 3     // 60 ms
#define SEG_EXIT_PROB    0.3f
#define SEG_EXIT_FRAMES  25    // 500 ms

struct mozart_dsp {
    // 高通 biquad 状态
    float hp_x1, hp_x2, hp_y1, hp_y2;
    // WOLA 降噪（单干核心）
    mozart_ns_t *ns;
    bool denoise_on;
    // 整形级
    mozart_gate_t    gate;
    mozart_agc_t     agc;
    mozart_limiter_t limiter;
    // 3:1 FIR 环形历史
    float fir_history[DECIMATOR_TAPS];
    int   fir_pos;
    // 便捷包装的输出累积（容纳初始化排干的突发）
    float wrapper_stage[MOZART_RAW_SAMPLES * 3];
    size_t wrapper_len;
    // 帧计数与 VAD 滞回
    uint32_t frame_idx;
    int  in_speech;
    int  enter_count;
    int  exit_count;
    uint8_t segment_id;
};

mozart_dsp_t *mozart_dsp_new(const mozart_dsp_config_t *cfg)
{
    mozart_dsp_t *d = calloc(1, sizeof(*d));
    if (!d) return NULL;
    d->denoise_on = cfg ? cfg->denoise : true;
    d->ns = mozart_ns_new(&(mozart_ns_config_t){ .bypass = !d->denoise_on });
    if (!d->ns) { free(d); return NULL; }
    mozart_gate_init(&d->gate, MOZART_RAW_SAMPLE_RATE);
    mozart_agc_init(&d->agc, MOZART_RAW_SAMPLE_RATE);
    mozart_limiter_init(&d->limiter, MOZART_RAW_SAMPLE_RATE);
    d->gate.enabled    = cfg ? cfg->gate    : false;
    d->agc.enabled     = cfg ? cfg->agc     : false;
    d->limiter.enabled = cfg ? cfg->limiter : false;
    return d;
}

void mozart_dsp_free(mozart_dsp_t *d)
{
    if (!d) return;
    mozart_ns_free(d->ns);
    free(d);
}

static void highpass_80hz(mozart_dsp_t *d, const float *in, float *out, size_t samples)
{
    for (size_t i = 0; i < samples; i++) {
        const float x = in[i];
        const float y = HP_B0 * x + HP_B1 * d->hp_x1 + HP_B2 * d->hp_x2
                        - HP_A1 * d->hp_y1 - HP_A2 * d->hp_y2;
        d->hp_x2 = d->hp_x1;
        d->hp_x1 = x;
        d->hp_y2 = d->hp_y1;
        d->hp_y1 = y;
        out[i] = y;
    }
}

size_t mozart_dsp_enhance(mozart_dsp_t *d, const float *in, size_t n,
                          float *out, size_t out_cap)
{
    if (!d || !in || !out) return (size_t)-1;
    if (n > out_cap) return (size_t)-1;
    if (n == 0) return 0;

    float hp[MOZART_RAW_SAMPLES];
    // 调用方按 ≤960 的块使用（主循环 period 或离线整帧）
    if (n > MOZART_RAW_SAMPLES) return (size_t)-1;
    highpass_80hz(d, in, hp, n);
    const size_t written = mozart_ns_push(d->ns, hp, n, out, out_cap);
    if (written == (size_t)-1) return (size_t)-1;
    mozart_gate_process(&d->gate, out, written, MOZART_RAW_SAMPLE_RATE);
    mozart_agc_process(&d->agc, out, written, MOZART_RAW_SAMPLE_RATE);
    mozart_limiter_process(&d->limiter, out, written, MOZART_RAW_SAMPLE_RATE);
    return written;
}

static void decimate_3x(mozart_dsp_t *d, const float *in, float *out)
{
    int out_pos = 0;
    for (int i = 0; i < MOZART_RAW_SAMPLES; i++) {
        d->fir_history[d->fir_pos] = in[i];
        d->fir_pos = (d->fir_pos + 1) % DECIMATOR_TAPS;
        if (i % 3 == 2) {
            float sum = 0.0f;
            int pos = d->fir_pos;
            for (int tap = 0; tap < DECIMATOR_TAPS; tap++) {
                pos = pos == 0 ? DECIMATOR_TAPS - 1 : pos - 1;
                sum += decimator_coeffs[tap] * d->fir_history[pos];
            }
            out[out_pos++] = sum;
        }
    }
}

static uint8_t energy_to_db(float rms)
{
    float db = 20.0f * log10f(rms + 1e-12f);          // -inf..0 dBFS
    float v = (db + 100.0f) * 2.55f;                  // -100..0 → 0..255
    if (v < 0.0f) v = 0.0f;
    if (v > 255.0f) v = 255.0f;
    return (uint8_t)v;
}

int mozart_dsp_pack_frame(mozart_dsp_t *d, const float *trunk_960,
                          mozart_input_frame_t *out)
{
    if (!d || !trunk_960 || !out) return -1;

    float dec[MOZART_INPUT_SAMPLES];
    decimate_3x(d, trunk_960, dec);
    for (int i = 0; i < MOZART_INPUT_SAMPLES; i++) {
        if (dec[i] > 1.0f) dec[i] = 1.0f;
        else if (dec[i] < -1.0f) dec[i] = -1.0f;
    }

    float power = 0.0f;
    for (int i = 0; i < MOZART_INPUT_SAMPLES; i++)
        power += dec[i] * dec[i];
    const uint8_t energy_db = energy_to_db(sqrtf(power / MOZART_INPUT_SAMPLES));

    // VAD 混合判据：WOLA 置信度（SNR 结构）× 主干绝对电平。
    // 纯 SNR 判据在停顿噪声地板处悬浮于退出门限附近，电平门限兜底。
    const float conf = (float)mozart_ns_speech_confidence(d->ns);
    float trunk_power = 0.0f;
    for (int i = 0; i < MOZART_RAW_SAMPLES; i++)
        trunk_power += trunk_960[i] * trunk_960[i];
    const float trunk_db = 20.0f * log10f(sqrtf(trunk_power / MOZART_RAW_SAMPLES) + 1e-12f);
    const bool audible = trunk_db > -60.0f;
    const int frame_vad = (conf >= 0.5f) && audible;
    if (d->in_speech) {
        const int quiet = (conf < SEG_EXIT_PROB) || !audible;
        d->exit_count = quiet ? d->exit_count + 1 : 0;
        if (d->exit_count >= SEG_EXIT_FRAMES) {
            d->in_speech = 0;
            d->exit_count = 0;
        }
    } else {
        const int ready = (conf >= SEG_ENTER_PROB) && audible;
        d->enter_count = ready ? d->enter_count + 1 : 0;
        if (d->enter_count >= SEG_ENTER_FRAMES) {
            d->in_speech = 1;
            d->enter_count = 0;
            d->segment_id = (uint8_t)(d->segment_id + 1); // 0 = 静音间隔
            if (d->segment_id == 0) d->segment_id = 1;
        }
    }

    out->meta.frame_idx  = ++d->frame_idx;
    out->meta.vad_flag   = (d->in_speech || frame_vad) ? 1 : 0;
    out->meta.energy_db  = energy_db;
    out->meta.conf       = (uint8_t)(conf * 255.0f + 0.5f);
    out->meta.segment_id = d->in_speech ? d->segment_id : 0;
    memcpy(out->pcm, dec, sizeof(dec));
    return 0;
}

int mozart_dsp_process(mozart_dsp_t *d,
                       const int16_t *stereo_s16,
                       mozart_input_frame_t *out)
{
    if (!d || !stereo_s16 || !out) return -1;

    float mono[MOZART_RAW_SAMPLES];
    for (int i = 0; i < MOZART_RAW_SAMPLES; i++)
        mono[i] = stereo_s16[2 * i] / 32768.0f;

    const size_t capacity = sizeof(d->wrapper_stage) / sizeof(float) - d->wrapper_len;
    const size_t written = mozart_dsp_enhance(d, mono, MOZART_RAW_SAMPLES,
                                              d->wrapper_stage + d->wrapper_len,
                                              capacity);
    if (written == (size_t)-1) return -1;
    d->wrapper_len += written;
    // 稳态下每 960 进 960 出；启动期输出滞后，不足 960 时补零打包保持节拍。
    if (d->wrapper_len < MOZART_RAW_SAMPLES) {
        memset(d->wrapper_stage + d->wrapper_len, 0,
               sizeof(float) * (MOZART_RAW_SAMPLES - d->wrapper_len));
    }
    const int error = mozart_dsp_pack_frame(d, d->wrapper_stage, out);
    // 滚动：保留超出 960 的溢出部分（初始化排干期出现）
    const size_t overflow = d->wrapper_len > MOZART_RAW_SAMPLES
                                ? d->wrapper_len - MOZART_RAW_SAMPLES : 0;
    if (overflow) memmove(d->wrapper_stage, d->wrapper_stage + MOZART_RAW_SAMPLES,
                          sizeof(float) * overflow);
    d->wrapper_len = overflow;
    return error;
}

void mozart_dsp_set_stages(mozart_dsp_t *d,
                           bool denoise, bool gate, bool agc, bool limiter)
{
    if (!d) return;
    // 运行时仅切增益旁路，保留滑窗和估计器，避免重新预热。
    d->denoise_on = denoise;
    mozart_ns_set_bypass(d->ns, !denoise);
    d->gate.enabled = gate;
    d->agc.enabled = agc;
    d->limiter.enabled = limiter;
}

void mozart_dsp_telemetry(const mozart_dsp_t *d, mozart_dsp_telemetry_t *out)
{
    if (!out) return;
    memset(out, 0, sizeof(*out));
    if (!d) return;
    out->denoise = d->denoise_on ? 1 : 0;
    out->gate = d->gate.enabled ? 1 : 0;
    out->agc = d->agc.enabled ? 1 : 0;
    out->limiter = d->limiter.enabled ? 1 : 0;
    out->gate_in_db = d->gate.in_peak_db;
    out->gate_out_db = d->gate.out_peak_db;
    out->gate_open = d->gate.open_count;
    out->gate_close = d->gate.close_count;
    out->agc_gain_db = d->agc.gain;
    out->agc_in_db = d->agc.in_rms_db;
    out->agc_out_db = d->agc.out_rms_db;
    out->limiter_in_db = d->limiter.in_peak_db;
    out->limiter_out_db = d->limiter.out_peak_db;
    out->ns_last_gain = (float)mozart_ns_last_gain(d->ns);
    out->ns_confidence = 0.0;   // 置信度查询有清零副作用，遥测里只报告增益
}

void mozart_dsp_reset(mozart_dsp_t *d)
{
    if (!d) return;
    d->hp_x1 = d->hp_x2 = d->hp_y1 = d->hp_y2 = 0.0f;
    memset(d->fir_history, 0, sizeof(d->fir_history));
    d->fir_pos = 0;
    mozart_ns_reset(d->ns);
    d->wrapper_len = 0;
    d->frame_idx = 0;
    d->in_speech = d->enter_count = d->exit_count = 0;
    d->segment_id = 0;
    const bool gate_on = d->gate.enabled, agc_on = d->agc.enabled,
               limiter_on = d->limiter.enabled;
    mozart_gate_init(&d->gate, MOZART_RAW_SAMPLE_RATE);
    mozart_agc_init(&d->agc, MOZART_RAW_SAMPLE_RATE);
    mozart_limiter_init(&d->limiter, MOZART_RAW_SAMPLE_RATE);
    d->gate.enabled = gate_on;
    d->agc.enabled = agc_on;
    d->limiter.enabled = limiter_on;
}
