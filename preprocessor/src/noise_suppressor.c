// noise_suppressor.c — WOLA 谱降噪 C 实现
// ============================================================================
// 结构与 Python 参考实现（tools/wola_reference.py）逐项对应：
//   滑窗分析（步进 hop=64、窗长 128、50% 重叠）；
//   噪声通道 = 慢 EMA + 频域平滑 + 最小值保持 + 门控慢上爬；
//   增益在发射时对最老帧用当前噪声估计计算（决策导向维纳）；
//   开局以首 8 帧功率均值初始化，初始化帧留在队列、就绪后一次排干；
//   每发射一帧产出 hop 个样本，输出与输入索引对齐。
// 内部 double 精度与参考实现（numpy float64）对齐。
#include "mozart/noise_suppressor.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#define NS_PI 3.14159265358979323846

#define NS_FRAME 128
#define NS_HOP 64
#define NS_BINS MOZART_NS_BINS
#define NS_INIT_FRAMES 8
#define NS_QUEUE_MAX 16   // 初始化积压 8 + 前瞻若干，16 足够

_Static_assert(NS_FRAME / 2 + 1 == MOZART_NS_BINS, "bin 数与窗长不匹配");

struct mozart_ns {
    // 配置（归一化后）
    double alpha_noise, beta_dd, noise_leak, leak_gate, floor;
    int    noise_smooth_bins;
    int    lookahead;
    bool   bypass;

    double window[NS_FRAME];
    double re[NS_FRAME], im[NS_FRAME];      // FFT 工作缓冲

    // 估计器状态
    bool   ready;
    int    init_count;
    double init_power[NS_BINS];
    double power_slow[NS_BINS];
    double noise[NS_BINS];
    double gain_prev[NS_BINS];

    // 发射队列：频谱（re/im）按帧序排队，发射时计算增益
    double queue_re[NS_QUEUE_MAX][NS_BINS];
    double queue_im[NS_QUEUE_MAX][NS_BINS];
    int    queue_len;

    // 滑窗输入与 WOLA 输出
    double in_buf[NS_FRAME];
    size_t in_len;
    double ola[NS_FRAME];
    double norm[NS_FRAME];

    // 遥测
    uint64_t consumed, emitted;
    double   conf_sum;
    uint64_t conf_count;
    double   last_gain;
};

// ---- 基础 FFT（迭代 radix-2，n 为 2 的幂） --------------------------------

static void fft_run(double *re, double *im, int n, int inverse)
{
    for (int i = 1, j = 0; i < n; i++) {
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            double t;
            t = re[i]; re[i] = re[j]; re[j] = t;
            t = im[i]; im[i] = im[j]; im[j] = t;
        }
    }
    for (int len = 2; len <= n; len <<= 1) {
        const double angle = (inverse ? 2.0 : -2.0) * NS_PI / (double)len;
        const double wr = cos(angle), wi = sin(angle);
        for (int i = 0; i < n; i += len) {
            double cr = 1.0, ci = 0.0;
            for (int k = 0; k < len / 2; k++) {
                const int a = i + k, b = a + len / 2;
                const double tr = cr * re[b] - ci * im[b];
                const double ti = cr * im[b] + ci * re[b];
                re[b] = re[a] - tr;
                im[b] = im[a] - ti;
                re[a] += tr;
                im[a] += ti;
                const double ncr = cr * wr - ci * wi;
                ci = cr * wi + ci * wr;
                cr = ncr;
            }
        }
    }
    if (inverse) {
        for (int i = 0; i < n; i++) {
            re[i] /= (double)n;
            im[i] /= (double)n;
        }
    }
}

// ---- 内部：频域平滑（两端复制延拓，对副本） --------------------------------

static void freq_smooth(const double *in, double *out, int radius)
{
    if (radius <= 0) {
        memcpy(out, in, sizeof(double) * NS_BINS);
        return;
    }
    const double denom = (double)(2 * radius + 1);
    for (int b = 0; b < NS_BINS; b++) {
        double sum = 0.0;
        for (int k = -radius; k <= radius; k++) {
            int idx = b + k;
            if (idx < 0) idx = 0;
            if (idx >= NS_BINS) idx = NS_BINS - 1;
            sum += in[idx];
        }
        out[b] = sum / denom;
    }
}

// ---- 内部：分析一帧（更新估计、频谱入队） ----------------------------------

static void ns_update(mozart_ns_t *ns)
{
    for (int i = 0; i < NS_FRAME; i++) {
        ns->re[i] = ns->window[i] * ns->in_buf[i];
        ns->im[i] = 0.0;
    }
    fft_run(ns->re, ns->im, NS_FRAME, 0);

    // 队列深度上界 = 初始化 8 + lookahead，恒小于 NS_QUEUE_MAX。
    for (int b = 0; b < NS_BINS; b++) {
        ns->queue_re[ns->queue_len][b] = ns->re[b];
        ns->queue_im[ns->queue_len][b] = ns->im[b];
    }
    ns->queue_len++;

    double power[NS_BINS];
    for (int b = 0; b < NS_BINS; b++)
        power[b] = ns->re[b] * ns->re[b] + ns->im[b] * ns->im[b];

    if (!ns->ready) {
        for (int b = 0; b < NS_BINS; b++) ns->init_power[b] += power[b];
        if (++ns->init_count >= NS_INIT_FRAMES) {
            for (int b = 0; b < NS_BINS; b++) {
                const double averaged = ns->init_power[b] / (double)NS_INIT_FRAMES;
                ns->power_slow[b] = averaged;
                ns->noise[b] = averaged;
            }
            ns->ready = true;
        }
        return;
    }

    for (int b = 0; b < NS_BINS; b++) {
        ns->power_slow[b] = ns->alpha_noise * ns->power_slow[b]
                            + (1.0 - ns->alpha_noise) * power[b];
    }
    double reference[NS_BINS];
    freq_smooth(ns->power_slow, reference, ns->noise_smooth_bins);
    for (int b = 0; b < NS_BINS; b++) {
        const double proposed = reference[b] < ns->leak_gate * ns->noise[b]
                                    ? ns->noise[b] * (1.0 + ns->noise_leak)
                                    : ns->noise[b];
        ns->noise[b] = proposed < reference[b] ? proposed : reference[b];
    }
}

// ---- 内部：发射最老帧（增益 + 合成 + 叠加），产出 hop 个样本 ----------------

static void ns_emit_oldest(mozart_ns_t *ns, float *out, size_t out_cap, size_t *written)
{
    double power[NS_BINS], gain[NS_BINS];
    double gain_sum = 0.0;
    double above = 0.0, total = 0.0;

    for (int b = 0; b < NS_BINS; b++) {
        power[b] = ns->queue_re[0][b] * ns->queue_re[0][b]
                   + ns->queue_im[0][b] * ns->queue_im[0][b];
        const double post = power[b] / (ns->noise[b] > 1e-20 ? ns->noise[b] : 1e-20);
        double xi;
        if (ns->emitted == 0) {
            xi = post - 1.0 > 0.0 ? post - 1.0 : 0.0;
        } else {
            xi = ns->beta_dd * ns->gain_prev[b] * ns->gain_prev[b] * post
                 + (1.0 - ns->beta_dd) * (post - 1.0 > 0.0 ? post - 1.0 : 0.0);
        }
        double g = xi / (1.0 + xi);
        if (g < ns->floor) g = ns->floor;
        if (ns->bypass) g = 1.0;
        gain[b] = g;
        ns->gain_prev[b] = g;
        gain_sum += g;
        total += power[b];
        if (post >= 2.0) above += power[b];
    }
    ns->last_gain = gain_sum / (double)NS_BINS;
    if (total > 1e-20) {
        ns->conf_sum += above / total;
        ns->conf_count++;
    }

    // 合成：增益乘谱、共轭对称补全、IFFT、加窗
    for (int b = 0; b < NS_BINS; b++) {
        ns->re[b] = ns->queue_re[0][b] * gain[b];
        ns->im[b] = ns->queue_im[0][b] * gain[b];
    }
    for (int i = 1; i < NS_FRAME / 2; i++) {
        ns->re[NS_FRAME - i] = ns->re[i];
        ns->im[NS_FRAME - i] = -ns->im[i];
    }
    fft_run(ns->re, ns->im, NS_FRAME, 1);
    for (int i = 0; i < NS_FRAME; i++) {
        ns->ola[i] += ns->window[i] * ns->re[i];
        ns->norm[i] += ns->window[i] * ns->window[i];
    }

    // 出队
    ns->queue_len--;
    memmove(ns->queue_re, ns->queue_re + 1, sizeof(double) * NS_BINS * (size_t)(NS_QUEUE_MAX - 1));
    memmove(ns->queue_im, ns->queue_im + 1, sizeof(double) * NS_BINS * (size_t)(NS_QUEUE_MAX - 1));

    // 产出 hop 个样本并滚动缓冲
    for (int i = 0; i < NS_HOP && *written < out_cap; i++) {
        out[(*written)++] = (float)(ns->ola[i] / (ns->norm[i] > 1e-9 ? ns->norm[i] : 1e-9));
        ns->emitted++;
    }
    memmove(ns->ola, ns->ola + NS_HOP, sizeof(double) * (NS_FRAME - NS_HOP));
    memmove(ns->norm, ns->norm + NS_HOP, sizeof(double) * (NS_FRAME - NS_HOP));
    memset(ns->ola + NS_FRAME - NS_HOP, 0, sizeof(double) * NS_HOP);
    memset(ns->norm + NS_FRAME - NS_HOP, 0, sizeof(double) * NS_HOP);
}

// ---- 公开接口 --------------------------------------------------------------

mozart_ns_t *mozart_ns_new(const mozart_ns_config_t *cfg)
{
    mozart_ns_t *ns = calloc(1, sizeof(*ns));
    if (!ns) return NULL;
    const int frame = cfg && cfg->frame ? cfg->frame : NS_FRAME;
    const int hop = cfg && cfg->hop ? cfg->hop : NS_HOP;
    const int lookahead = cfg ? cfg->lookahead : 0;
    if (frame != NS_FRAME || hop != NS_HOP || lookahead < 0
        || 1 + lookahead >= NS_QUEUE_MAX) {
        free(ns);
        return NULL;
    }
    ns->alpha_noise = cfg && cfg->alpha_noise ? cfg->alpha_noise : 0.99;
    ns->beta_dd = cfg && cfg->beta_dd ? cfg->beta_dd : 0.98;
    ns->noise_leak = cfg && cfg->noise_leak ? cfg->noise_leak : 0.0004;
    ns->leak_gate = cfg && cfg->leak_gate ? cfg->leak_gate : 3.0;
    ns->floor = cfg && cfg->floor_db
        ? pow(10.0, cfg->floor_db / 20.0)
        : pow(10.0, -18.0 / 20.0);
    ns->noise_smooth_bins = cfg && cfg->noise_smooth_bins ? cfg->noise_smooth_bins : 1;
    ns->bypass = cfg ? cfg->bypass : false;
    ns->lookahead = lookahead;

    for (int b = 0; b < NS_BINS; b++) ns->gain_prev[b] = 1.0;

    // 周期 sqrt-Hann：hann(i) = 0.5 - 0.5*cos(2πi/N)，i = 0..N-1
    // 与 numpy hanning(N+1)[:N] 一致。
    for (int i = 0; i < NS_FRAME; i++) {
        const double hann = 0.5
            - 0.5 * cos(2.0 * NS_PI * (double)i / (double)NS_FRAME);
        ns->window[i] = sqrt(hann);
    }
    return ns;
}

size_t mozart_ns_push(mozart_ns_t *ns, const float *in, size_t n,
                      float *out, size_t out_cap)
{
    if (!ns || !in || !out) return (size_t)-1;
    size_t written = 0;
    for (size_t pos = 0; pos < n; pos++) {
        ns->in_buf[ns->in_len++] = (double)in[pos];
        ns->consumed++;
        if (ns->in_len < (size_t)NS_FRAME) continue;
        ns_update(ns);
        // 滑窗：丢弃最老 hop，保留 frame-hop 的重叠
        memmove(ns->in_buf, ns->in_buf + NS_HOP,
                sizeof(double) * (NS_FRAME - NS_HOP));
        ns->in_len = NS_FRAME - NS_HOP;
        // 就绪后排干队列到 lookahead 深度（初始化积压一次清空）
        while (ns->ready && ns->queue_len > ns->lookahead) {
            ns_emit_oldest(ns, out, out_cap, &written);
        }
    }
    return written;
}

size_t mozart_ns_flush(mozart_ns_t *ns, float *out, size_t out_cap)
{
    if (!ns || !out) return (size_t)-1;
    size_t written = 0;
    // 与参考实现一致：补零推入约两个帧长让滑窗越过残留样本，再全量排空队列。
    const size_t pad = (size_t)(2 * NS_FRAME);
    for (size_t i = 0; i < pad; i++) {
        ns->in_buf[ns->in_len++] = 0.0;
        if (ns->in_len < (size_t)NS_FRAME) continue;
        ns_update(ns);
        memmove(ns->in_buf, ns->in_buf + NS_HOP,
                sizeof(double) * (NS_FRAME - NS_HOP));
        ns->in_len = NS_FRAME - NS_HOP;
        while (ns->ready && ns->queue_len > ns->lookahead)
            ns_emit_oldest(ns, out, out_cap, &written);
    }
    while (ns->ready && ns->queue_len > 0 && written < out_cap)
        ns_emit_oldest(ns, out, out_cap, &written);
    return written;
}

double mozart_ns_speech_confidence(mozart_ns_t *ns)
{
    if (!ns || ns->conf_count == 0) return 0.0;
    const double value = ns->conf_sum / (double)ns->conf_count;
    ns->conf_sum = 0.0;
    ns->conf_count = 0;
    return value;
}

double mozart_ns_last_gain(const mozart_ns_t *ns)
{
    return ns ? ns->last_gain : 0.0;
}

uint64_t mozart_ns_consumed(const mozart_ns_t *ns)
{
    return ns ? ns->consumed : 0;
}

uint64_t mozart_ns_emitted(const mozart_ns_t *ns)
{
    return ns ? ns->emitted : 0;
}

void mozart_ns_free(mozart_ns_t *ns)
{
    free(ns);
}
