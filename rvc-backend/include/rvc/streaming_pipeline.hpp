#pragma once

// streaming_pipeline.hpp — 实时滑动窗口流式 RVC
// ============================================================================
// 把 20ms 契约帧流改造成 RVC 分块推理。静态 Generator 使用旧的 2s
// 滑窗；动态显式噪声 Generator 可使用与 Golden 相同的 full-history +
// 2s lookahead correctness profile；upstream realtime 模式使用短 block、
// 固定过去上下文和 SOLA，不等待 future audio。
//
//   push(320样本帧) ──→ 样本级环形缓冲 ──→ 推理线程取"最新 2s 窗"
//                                             ↓ pipeline.process(32000)
//                                          96000 样本输出 @48k
//                                             ↓ 最小交叉淡化（50ms）拼接
//   pop_output(960样本帧) ←── 输出环形缓冲 ←─┘
//
// 延迟预算：冷启动 2s 攒窗；稳态 ≈ 50ms 淡化 + 单次推理耗时。
// 跨块拼接：块间输入 hop = window - crossfade/3（输出 50ms 重叠区线性淡化）。
//
// 不连续性处理（宁可重置也不接错相位）：
//   - frame_idx 跳变 / segment_id 变化 / pts 大跳 → 全量重置（清环、清淡化尾）
//   - 推理落后（积压 > 2 hop）→ 丢弃整块直接追最新，淡化尾作废
//   - 静音窗（skip_silence 且整窗无 VAD）→ 跳过推理，输出等长静音

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <vector>

#include "mozart/frame_meta.h"
#include "rvc/pipeline.hpp"

namespace rvc {

class StreamingRvc {
public:
    struct Config {
        // 输入侧（16k）：窗口必须 = 32000（T=200 特征帧对应 2s 音频）
        size_t window_samples = 32000;
        // 输出侧（48k）块间交叉淡化长度。约束：crossfade_out/3 必须使
        // hop = window - crossfade/3 为帧长(320)的整数倍，否则触发点被
        // 帧量化漂移、窗口错位。2880/3=960 → hop=31040=97 帧 ✓（60ms）
        size_t crossfade_out = 2880;
        bool skip_silence = true;
        bool full_history = false;
        bool upstream_realtime = false;
        size_t past_context_samples = 40000;
        size_t sola_search_out = 480;
        size_t right_context_samples = 0;
        size_t guard_samples = 0;
        size_t max_history_samples = 41 * 16000;
        // Keep this many complete output hops queued before playback starts.
        // Quality mode uses extra reserve hops because prefix inference gets
        // slower as history grows.
        size_t startup_buffer_blocks = 1;
    };

    struct Stats {
        std::atomic<uint64_t> blocks{0};            // 完成推理的块数
        std::atomic<uint64_t> skipped_blocks{0};    // 静音跳过的块数
        std::atomic<uint64_t> resets{0};            // 不连续重置次数
        std::atomic<uint64_t> late_blocks{0};       // 推理落后丢弃追最新的次数
        std::atomic<uint64_t> input_overruns{0};    // 输入环溢出丢样本
        std::atomic<uint64_t> output_overruns{0};   // 输出环满丢样本（消费端太慢）
        std::atomic<uint64_t> inference_errors{0};  // 推理异常次数
    };

    explicit StreamingRvc(Config config);

    // ---- AudioWorker 泵线程 ----
    // 喂一帧；内部检测 frame_idx/segment_id/pts 不连续并触发重置。
    void push(const mozart_input_frame_t& frame);

    // 取最多 n 个输出样本，返回实际取到的数量（不足由调用方补零=欠载）。
    size_t pop_output(float* dst, size_t n);

    // ---- 推理线程 ----
    // 阻塞循环：攒窗 → process → 交叉淡化 → 输出环。
    void inference_loop(RVCPipelineBase& pipeline, const std::atomic<bool>& running);

    // 非阻塞单步（供测试驱动）：有待处理窗则处理一块并返回 true。
    bool try_process_one(RVCPipelineBase& pipeline);

    const Stats& stats() const noexcept { return stats_; }
    size_t output_pending() const;

private:
    Config cfg_;
    Stats stats_;

    const size_t hop_in_;        // 窗口推进步长（输入样本）
    const size_t emit_out_;      // 每块固定输出的样本数（= hop_in * 3）
    const size_t out_block_;     // 单块推理输出长度（= window * 3）
    const size_t analysis_samples_;

    // 输入样本环（容量 = 4 窗，推理落后时丢最旧）
    std::mutex ring_mutex_;
    std::vector<float> ring_;
    size_t ring_head_ = 0;       // 环内最旧样本下标
    size_t ring_size_ = 0;
    uint64_t pushed_total_ = 0;  // 累计入环样本；由 cv_mutex_ 保护

    // 窗口调度状态（cv_mutex_ 保护）
    std::mutex cv_mutex_;
    std::condition_variable cv_;
    uint64_t owned_ = 0;         // 上次取窗时已见到的 pushed_total_
    uint64_t next_window_start_ = 0;
    bool have_window_base_ = false;
    bool window_voiced_ = false; // 自上次取窗以来出现过 VAD
    std::atomic<uint64_t> reset_gen_{0};     // 重置代数（push 侧递增，推理侧对齐）

    // 不连续检测基准（push 侧独占）
    bool have_last_meta_ = false;
    uint32_t last_frame_idx_ = 0;
    uint8_t last_segment_ = 0;
    uint64_t last_pts_ns_ = 0;

    // 输出样本环
    mutable std::mutex out_mutex_;
    std::vector<float> out_ring_;
    size_t out_head_ = 0;
    size_t out_size_ = 0;
    uint64_t out_total_ = 0;
    bool output_started_ = false;

    // 块间淡化尾（crossfade_out 样本），cv_mutex_ 保护（仅推理线程读写）
    std::vector<float> tail_;
    bool tail_valid_ = false;    // false = 下一块直接输出不混合

    void handle_discontinuity();
    void read_last_window(size_t count, std::vector<float>& dst);
    bool read_prefix(size_t count, std::vector<float>& dst);
    void push_output(const float* data, size_t n);
    void push_output_zeros(size_t n);
};

} // namespace rvc
