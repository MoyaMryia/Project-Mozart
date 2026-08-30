// dsp.c — 采集帧 → 16k 契约帧 DSP 链
// ============================================================================
// 见 dsp.h 顶部说明。所有有状态：高通 biquad、RNNoise、3:1 FIR 历史、
// VAD 滞回、frame_idx，均集中在本模块，mozart_dsp_reset() 一键清空。
#include "mozart/dsp.h"
#include "mozart/rnnoise.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#define RNNOISE_FRAME_SAMPLES 480
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
     0.0000000000e+00f,  1.3328130710e-02f,  2.0914031268e-02f,
     8.9839746223e-03f, -2.2538271381e-02f, -5.0922926864e-02f,
    -4.0588192824e-02f,  3.0283637766e-02f,  1.4611043959e-01f,
     2.5519851986e-01f,  2.9990509072e-01f,  2.5519851986e-01f,
     1.4611043959e-01f,  3.0283637766e-02f, -4.0588192824e-02f,
    -5.0922926864e-02f, -2.2538271381e-02f,  8.9839746223e-03f,
     2.0914031268e-02f,  1.3328130710e-02f,  0.0000000000e+00f,
    -7.0800492720e-03f, -5.8142304759e-03f, -1.2652814003e-03f,
     1.5189746882e-03f,  1.4895510869e-03f,  4.2914726915e-04f
};

// VAD 滞回参数（20 ms/帧）
#define SEG_ENTER_PROB   0.6f
#define SEG_ENTER_FRAMES 3     // 60 ms
#define SEG_EXIT_PROB    0.3f
#define SEG_EXIT_FRAMES  25    // 500 ms
#define VAD_FRAME_PROB   0.5f  // 帧级 vad_flag 门限
#define ENERGY_GATE_DB   -60.f // 无 RNNoise 时的能量开门限（dBFS）

struct mozart_dsp {
    // 高通 biquad 状态
    float hp_x1, hp_x2, hp_y1, hp_y2;
    // 3:1 FIR 环形历史
    float fir_history[DECIMATOR_TAPS];
    int   fir_pos;
    // RNNoise（可空 = 直通）
    mozart_rnnoise_t *rn;
    // 帧计数
    uint32_t frame_idx;
    // VAD / 分段滞回状态
    int  in_speech;
    int  enter_count;
    int  exit_count;
    uint8_t segment_id;
    // 能量门限平滑（无 RNNoise 时用）
    float fast_db, slow_db;
};

mozart_dsp_t *mozart_dsp_new(const mozart_dsp_config_t *cfg)
{
    mozart_dsp_t *d = calloc(1, sizeof(*d));
    if (!d) return NULL;
    bool use_rn = cfg ? cfg->rnnoise : true;
    if (use_rn) {
        d->rn = mozart_rnnoise_new(cfg ? cfg->rnnoise_model : NULL);
        if (!d->rn) { free(d); return NULL; }
    }
    return d;
}

void mozart_dsp_free(mozart_dsp_t *d)
{
    if (!d) return;
    mozart_rnnoise_free(d->rn);
    free(d);
}

void mozart_dsp_reset(mozart_dsp_t *d)
{
    if (!d) return;
    d->hp_x1 = d->hp_x2 = d->hp_y1 = d->hp_y2 = 0.0f;
    memset(d->fir_history, 0, sizeof(d->fir_history));
    d->fir_pos = 0;
    mozart_rnnoise_reset(d->rn);
    d->frame_idx = 0;
    d->in_speech = d->enter_count = d->exit_count = 0;
    d->segment_id = 0;
    d->fast_db = d->slow_db = -100.0f;
}

static void highpass_80hz(mozart_dsp_t *d, const float *in, float *out,
                          int samples)
{
    for (int i = 0; i < samples; i++) {
        float x = in[i];
        float y = HP_B0 * x + HP_B1 * d->hp_x1 + HP_B2 * d->hp_x2
                  - HP_A1 * d->hp_y1 - HP_A2 * d->hp_y2;
        d->hp_x2 = d->hp_x1;
        d->hp_x1 = x;
        d->hp_y2 = d->hp_y1;
        d->hp_y1 = y;
        out[i] = y;
    }
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

int mozart_dsp_process(mozart_dsp_t *d,
                       const int16_t *stereo_s16,
                       mozart_input_frame_t *out)
{
    if (!d || !stereo_s16 || !out) return -1;

    // 1) 取 FL 通道，S16 → f32
    float mono[MOZART_RAW_SAMPLES];
    for (int i = 0; i < MOZART_RAW_SAMPLES; i++)
        mono[i] = stereo_s16[2 * i] / 32768.0f;

    // 2) 80 Hz 高通
    float hp[MOZART_RAW_SAMPLES];
    highpass_80hz(d, mono, hp, MOZART_RAW_SAMPLES);

    // 3) RNNoise 全湿（2×480 子帧），帧级 VAD 概率取两者最大值。
    //    d->rn == NULL（旁路）时直通，VAD 退化为能量门限（见步骤 6）。
    float den[MOZART_RAW_SAMPLES];
    float vad_prob = 0.0f;
    if (d->rn) {
        for (int off = 0; off < MOZART_RAW_SAMPLES; off += RNNOISE_FRAME_SAMPLES) {
            float p = 0.0f;
            int rc = mozart_rnnoise_process(d->rn, hp + off, den + off, &p);
            if (rc < 0) return rc;
            if (p > vad_prob) vad_prob = p;
        }
    } else {
        memcpy(den, hp, sizeof(hp));
    }

    // 4) 3:1 降采样 → 16 kHz；限幅 ±1.0（RNNoise 偶发过冲 >1）
    float dec[MOZART_INPUT_SAMPLES];
    decimate_3x(d, den, dec);
    for (int i = 0; i < MOZART_INPUT_SAMPLES; i++) {
        if (dec[i] > 1.0f) dec[i] = 1.0f;
        else if (dec[i] < -1.0f) dec[i] = -1.0f;
    }

    // 5) 帧能量（dBFS → 0..255）
    float power = 0.0f;
    for (int i = 0; i < MOZART_INPUT_SAMPLES; i++)
        power += dec[i] * dec[i];
    uint8_t energy_db = energy_to_db(sqrtf(power / MOZART_INPUT_SAMPLES));

    // 6) VAD / 分段滞回
    int frame_vad;
    if (d->rn) {
        frame_vad = vad_prob >= VAD_FRAME_PROB;
        if (d->in_speech) {
            d->exit_count = vad_prob < SEG_EXIT_PROB ? d->exit_count + 1 : 0;
            if (d->exit_count >= SEG_EXIT_FRAMES) {
                d->in_speech = 0;
                d->exit_count = 0;
            }
        } else {
            d->enter_count = vad_prob >= SEG_ENTER_PROB ? d->enter_count + 1 : 0;
            if (d->enter_count >= SEG_ENTER_FRAMES) {
                d->in_speech = 1;
                d->enter_count = 0;
                d->segment_id = (uint8_t)(d->segment_id + 1); // 0 = 静音间隔
            }
        }
    } else {
        // 无 RNNoise：能量双门限退路
        float db = 20.0f * log10f(sqrtf(power / MOZART_INPUT_SAMPLES) + 1e-12f);
        d->slow_db += 0.02f * (db - d->slow_db);
        d->fast_db += 0.50f * (db - d->fast_db);
        frame_vad = db > ENERGY_GATE_DB && db > d->slow_db + 6.0f;
        if (frame_vad && !d->in_speech) {
            d->in_speech = 1;
            d->segment_id = (uint8_t)(d->segment_id + 1);
        } else if (!frame_vad && d->in_speech && db < d->slow_db + 3.0f) {
            d->in_speech = 0;
        }
    }

    // 7) 填契约帧
    out->meta.frame_idx  = ++d->frame_idx;
    out->meta.vad_flag   = (d->in_speech || frame_vad) ? 1 : 0;
    out->meta.energy_db  = energy_db;
    out->meta.conf       = (uint8_t)(vad_prob * 255.0f + 0.5f);
    out->meta.segment_id = d->in_speech ? d->segment_id : 0;
    memcpy(out->pcm, dec, sizeof(dec));
    return 0;
}
