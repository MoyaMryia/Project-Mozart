// noise_suppressor.h — WOLA 谱降噪（48 kHz 单声道 f32，流式）
// ============================================================================
// Python 参考实现（tools/wola_reference.py）的 C 移植，参数与流式语义一致；
// 对拍夹具由 tools/gen_ns_fixture.py 生成。默认配置 frame=128 / hop=64 /
// lookahead=0，稳态发射延迟 192 样本（4 ms）。
// 输出与输入索引对齐（WOLA 重建无位移）；push 任意长度分块与整块结果一致。
#ifndef MOZART_NOISE_SUPPRESSOR_H
#define MOZART_NOISE_SUPPRESSOR_H

#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MOZART_NS_BINS 65   // frame 128 的一半加一

typedef struct mozart_ns mozart_ns_t;

typedef struct {
    int    frame;             // 分析窗长，默认 128（当前实现固定 128）
    int    hop;               // 步进，默认 64
    int    lookahead;         // 前瞻帧数，默认 0
    double alpha_noise;       // 噪声通道慢 EMA，默认 0.99
    double beta_dd;           // 决策导向先验 SNR 平滑，默认 0.98
    double noise_leak;        // 门控上爬速率，默认 0.0004
    double leak_gate;         // 上爬门控比，默认 3.0
    double floor_db;          // 增益地板，默认 -18
    int    noise_smooth_bins; // 噪声参考谱频域平滑半径，默认 1
    bool   bypass;            // true = 增益恒 1（WOLA 直通）
} mozart_ns_config_t;

/** 创建。cfg 为 NULL 时取全默认；字段为 0 时取该项默认。失败返回 NULL。 */
mozart_ns_t *mozart_ns_new(const mozart_ns_config_t *cfg);

/**
 * 流式处理：push n 个输入样本，向 out 写至多 out_cap 个样本。
 * 输出与输入索引对齐；样本未攒够一 hop 时本次可能零输出。
 * @return 实际写入 out 的样本数；ns/out 为 NULL 或 n<0 返回 SIZE_MAX。
 */
size_t mozart_ns_push(mozart_ns_t *ns, const float *in, size_t n,
                      float *out, size_t out_cap);

/** 尾部补零排空。语义同 push。 */
size_t mozart_ns_flush(mozart_ns_t *ns, float *out, size_t out_cap);

/**
 * 自上次查询以来的语音置信度（0..1）：功率加权的 post-SNR ≥ 2 的 bin 占比。
 * 查询后清零累积。供 VAD 滞回使用。
 */
double mozart_ns_speech_confidence(mozart_ns_t *ns);

/** 最近一次发射帧的平均增益（0..1），遥测用。 */
double mozart_ns_last_gain(const mozart_ns_t *ns);

/** 累计已消费输入样本数与已发射输出样本数（发射延迟遥测用）。 */
uint64_t mozart_ns_consumed(const mozart_ns_t *ns);
uint64_t mozart_ns_emitted(const mozart_ns_t *ns);

void mozart_ns_free(mozart_ns_t *ns);

#ifdef __cplusplus
}
#endif

#endif // MOZART_NOISE_SUPPRESSOR_H
