// streaming_pipeline.cpp — 实时滑动窗口流式 RVC 实现（见头文件说明）
#include "rvc/streaming_pipeline.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <stdexcept>
#include <utility>

#include <spdlog/spdlog.h>
#include <chrono>

namespace rvc {

namespace {
constexpr uint64_t kPtsResetGapNs = 200000000ull; // pts 跳变 >200ms 视为不连续
constexpr uint32_t kFrameIdxResetGap = 4;        // frame_idx 缺口 >4 帧视为不连续
constexpr size_t kOutRingCapacity = 96000 * 8;   // 16s @48k（推理可领先消费端数块）
} // namespace

StreamingRvc::StreamingRvc(Config config)
    : cfg_(std::move(config)),
      hop_in_(cfg_.window_samples - cfg_.crossfade_out / 3),
      emit_out_(hop_in_ * 3),
      out_block_(cfg_.window_samples * 3)
{
    if (cfg_.window_samples < 200 || cfg_.crossfade_out % 3 != 0
        || cfg_.crossfade_out >= cfg_.window_samples
        || cfg_.startup_buffer_blocks == 0
        || cfg_.startup_buffer_blocks > kOutRingCapacity / emit_out_) {
        throw std::invalid_argument("StreamingRvc: invalid window/crossfade config");
    }
    ring_.resize(cfg_.full_history
        ? cfg_.max_history_samples
        : cfg_.window_samples * 4);
    out_ring_.resize(kOutRingCapacity);
    tail_.assign(cfg_.crossfade_out, 0.0f);
}

void StreamingRvc::handle_discontinuity()
{
    std::lock_guard<std::mutex> lk(cv_mutex_);
    std::lock_guard<std::mutex> ring_lk(ring_mutex_);
    ring_head_ = ring_size_ = 0;
    pushed_total_ = 0;
    next_window_start_ = 0;
    have_window_base_ = false;
    window_voiced_ = false;
    // tail_/tail_valid_ 归推理线程所有：它检测到代数变化后自行作废
    ++reset_gen_;
    stats_.resets.fetch_add(1, std::memory_order_relaxed);
    {
        std::lock_guard<std::mutex> out_lk(out_mutex_);
        out_head_ = out_size_ = 0;
        output_started_ = false;
    }
}

void StreamingRvc::push(const mozart_input_frame_t& frame)
{
    // ---- 不连续检测（push 侧独占状态）----
    if (have_last_meta_) {
        const uint32_t idx_gap = frame.meta.frame_idx - last_frame_idx_; // u32 回绕安全
        const bool idx_gap_bad = idx_gap == 0 || idx_gap > kFrameIdxResetGap;
        const uint8_t seg = frame.meta.segment_id;
        const bool seg_changed = seg != last_segment_;
        const bool pts_jump = frame.meta.pts_ns > last_pts_ns_
            && frame.meta.pts_ns - last_pts_ns_ > kPtsResetGapNs;
        if (idx_gap_bad || seg_changed || pts_jump) {
            handle_discontinuity();
        }
    }
    last_frame_idx_ = frame.meta.frame_idx;
    last_segment_ = frame.meta.segment_id;
    last_pts_ns_ = frame.meta.pts_ns;
    have_last_meta_ = true;

    // ---- 入环 ----
    {
        std::lock_guard<std::mutex> lk(ring_mutex_);
        for (int i = 0; i < MOZART_INPUT_SAMPLES; ++i) {
            if (ring_size_ == ring_.size()) {
                ring_head_ = (ring_head_ + 1) % ring_.size();
                --ring_size_;
                stats_.input_overruns.fetch_add(1, std::memory_order_relaxed);
            }
            ring_[(ring_head_ + ring_size_) % ring_.size()] = frame.pcm[i];
            ++ring_size_;
        }
    }

    std::lock_guard<std::mutex> lk(cv_mutex_);
    pushed_total_ += MOZART_INPUT_SAMPLES;
    if (frame.meta.vad_flag != 0) window_voiced_ = true;
    cv_.notify_one();
}

void StreamingRvc::read_last_window(std::vector<float>& dst)
{
    std::lock_guard<std::mutex> lk(ring_mutex_);
    dst.assign(cfg_.window_samples, 0.0f);
    const size_t take = std::min(ring_size_, cfg_.window_samples);
    const size_t dst_off = cfg_.window_samples - take;
    const size_t src_off = ring_size_ - take;
    for (size_t i = 0; i < take; ++i) {
        dst[dst_off + i] = ring_[(ring_head_ + src_off + i) % ring_.size()];
    }
    // Samples older than the latest complete window are normal rolling
    // history, not an overrun. Pruning here leaves the extra capacity solely
    // for genuine inference backlog.
    if (ring_size_ > take) {
        const size_t discard = ring_size_ - take;
        ring_head_ = (ring_head_ + discard) % ring_.size();
        ring_size_ = take;
    }
}

bool StreamingRvc::read_prefix(size_t count, std::vector<float>& dst)
{
    std::lock_guard<std::mutex> lk(ring_mutex_);
    if (ring_head_ != 0 || ring_size_ < count) return false;
    dst.assign(ring_.begin(), ring_.begin() + static_cast<std::ptrdiff_t>(count));
    return true;
}

bool StreamingRvc::try_process_one(RVCPipelineBase& pipeline)
{
    std::vector<float> window;
    bool stale_tail = false;
    bool voiced = false;
    uint64_t pushed_at_window = 0;
    uint64_t window_start = 0;

    {
        std::unique_lock<std::mutex> lk(cv_mutex_);

        // 等待窗口就绪（try_process_one 语义：不等待，仅检查）
        if (cfg_.full_history) {
            const uint64_t required = next_window_start_ + cfg_.window_samples
                + cfg_.right_context_samples + cfg_.guard_samples;
            if (required > cfg_.max_history_samples || pushed_total_ < required) {
                return false;
            }
        } else if (have_window_base_) {
            if (pushed_total_ - owned_ < hop_in_) return false;
        } else {
            if (pushed_total_ < cfg_.window_samples) return false;
        }

        // 推理落后超过 2 hop：放弃旧块节奏，直接追最新，淡化尾作废
        if (!cfg_.full_history && have_window_base_
            && pushed_total_ - owned_ > 2 * hop_in_) {
            stale_tail = true;
            stats_.late_blocks.fetch_add(1, std::memory_order_relaxed);
        }

        if (cfg_.full_history) {
            window_start = next_window_start_;
            const size_t inference_end = static_cast<size_t>(window_start)
                + cfg_.window_samples + cfg_.right_context_samples
                + cfg_.guard_samples;
            if (!read_prefix(inference_end, window)) return false;
            next_window_start_ += hop_in_;
        } else {
            read_last_window(window);
        }
        voiced = window_voiced_;
        window_voiced_ = false;
        owned_ = pushed_total_;
        pushed_at_window = pushed_total_;
        have_window_base_ = true;
    }

    // ---- 静音窗：跳过推理，输出等长静音 ----
    if (cfg_.skip_silence && !voiced) {
        static std::atomic<uint64_t> skip_logged{0};
        if (skip_logged.fetch_add(1) < 3) {
            spdlog::warn("[stream] window skipped as silent (pushed={})", pushed_at_window);
        }
        std::fill(tail_.begin(), tail_.end(), 0.0f);
        tail_valid_ = false;
        push_output_zeros(emit_out_);
        stats_.skipped_blocks.fetch_add(1, std::memory_order_relaxed);
        return true;
    }

    // ---- 推理（耗时操作，锁外执行）----
    std::vector<float> converted;
    const auto t_infer = std::chrono::steady_clock::now();
    try {
        converted = pipeline.process(window);
    } catch (const std::exception& e) {
        stats_.inference_errors.fetch_add(1, std::memory_order_relaxed);
        static std::atomic<uint64_t> logged{0};
        if (logged.fetch_add(1) < 5) {
            spdlog::warn("streaming inference failed: {}", e.what());
        }
        std::fill(tail_.begin(), tail_.end(), 0.0f);
        tail_valid_ = false;
        push_output_zeros(emit_out_);
        return true;
    }

    if (cfg_.full_history) {
        const size_t output_start = static_cast<size_t>(window_start) * 3;
        if (converted.size() < output_start + out_block_) {
            stats_.inference_errors.fetch_add(1, std::memory_order_relaxed);
            spdlog::warn(
                "quality streaming: prefix returned {} samples, need at least {}",
                converted.size(), output_start + out_block_);
            std::fill(tail_.begin(), tail_.end(), 0.0f);
            tail_valid_ = false;
            push_output_zeros(emit_out_);
            return true;
        }
        converted = std::vector<float>(
            converted.begin() + static_cast<std::ptrdiff_t>(output_start),
            converted.begin() + static_cast<std::ptrdiff_t>(output_start + out_block_));
    }

    if (converted.size() < out_block_) {
        stats_.inference_errors.fetch_add(1, std::memory_order_relaxed);
        spdlog::warn("streaming: generator returned {} samples, expect {}",
                     converted.size(), out_block_);
        std::fill(tail_.begin(), tail_.end(), 0.0f);
        tail_valid_ = false;
        push_output_zeros(emit_out_);
        return true;
    }

    // ---- 最小交叉淡化拼接 ----
    if (stale_tail) {
        std::fill(tail_.begin(), tail_.end(), 0.0f);
        tail_valid_ = false;
    }
    const size_t cf = cfg_.crossfade_out;
    std::vector<float> emit;
    emit.resize(emit_out_);

    if (!tail_valid_) {
        // 首块或尾已作废：直接输出（下一个块起恢复淡化衔接）
        std::copy_n(converted.begin(), emit_out_, emit.begin());
    } else {
        for (size_t i = 0; i < cf; ++i) {
            const float w = static_cast<float>(i) / static_cast<float>(cf);
            emit[i] = tail_[i] * (1.0f - w) + converted[i] * w;
        }
        std::copy_n(converted.begin() + static_cast<std::ptrdiff_t>(cf),
                    emit_out_ - cf,
                    emit.begin() + static_cast<std::ptrdiff_t>(cf));
    }
    // 新淡化尾 = 本块最后 cf 个样本（与下一块前 cf 对应同一段输入）
    std::copy_n(converted.begin() + static_cast<std::ptrdiff_t>(out_block_ - cf),
                cf, tail_.begin());
    tail_valid_ = true;

    push_output(emit.data(), emit.size());
    stats_.blocks.fetch_add(1, std::memory_order_relaxed);
    {
        static std::atomic<uint64_t> blk_logged{0};
        if (blk_logged.fetch_add(1) < 3) {
            spdlog::info("[stream] block {} done: infer={}ms out={} samples",
                         blk_logged.load(),
                         std::chrono::duration<double, std::milli>(
                             std::chrono::steady_clock::now() - t_infer).count(),
                         converted.size());
        }
    }
    return true;
}

void StreamingRvc::inference_loop(RVCPipelineBase& pipeline,
                                  const std::atomic<bool>& running)
{
    spdlog::info(
        "StreamingRvc inference thread started: window={} hop={} crossfade={} "
        "full_history={} right_context={} guard={}",
        cfg_.window_samples, hop_in_, cfg_.crossfade_out, cfg_.full_history,
        cfg_.right_context_samples, cfg_.guard_samples);
    uint64_t seen_reset = reset_gen_.load();
    while (running.load(std::memory_order_relaxed)) {
        {
            std::unique_lock<std::mutex> lk(cv_mutex_);
            if (reset_gen_.load() != seen_reset) {
                seen_reset = reset_gen_.load();
                std::fill(tail_.begin(), tail_.end(), 0.0f);
                tail_valid_ = false;
                continue;
            }
            const uint64_t required = next_window_start_ + cfg_.window_samples
                + cfg_.right_context_samples + cfg_.guard_samples;
            const bool ready = cfg_.full_history
                ? required <= cfg_.max_history_samples && pushed_total_ >= required
                : (have_window_base_
                    ? pushed_total_ - owned_ >= hop_in_
                    : pushed_total_ >= cfg_.window_samples);
            if (!ready) {
                cv_.wait_for(lk, std::chrono::milliseconds(50));
                continue;
            }
        }
        if (reset_gen_.load() != seen_reset) {
            seen_reset = reset_gen_.load();
            continue;
        }
        try_process_one(pipeline);
    }
    spdlog::info("StreamingRvc inference thread stopped: blocks={} skipped={} resets={}",
                 stats_.blocks.load(), stats_.skipped_blocks.load(),
                 stats_.resets.load());
}

size_t StreamingRvc::pop_output(float* dst, size_t n)
{
    std::lock_guard<std::mutex> lk(out_mutex_);
    if (!output_started_) {
        if (out_size_ < cfg_.startup_buffer_blocks * emit_out_) return 0;
        output_started_ = true;
    }
    const size_t got = std::min(n, out_size_);
    for (size_t i = 0; i < got; ++i) {
        dst[i] = out_ring_[out_head_];
        out_head_ = (out_head_ + 1) % kOutRingCapacity;
    }
    out_size_ -= got;
    return got;
}

size_t StreamingRvc::output_pending() const
{
    std::lock_guard<std::mutex> lk(out_mutex_);
    return out_size_;
}

void StreamingRvc::push_output(const float* data, size_t n)
{
    std::lock_guard<std::mutex> lk(out_mutex_);
    for (size_t i = 0; i < n; ++i) {
        if (out_size_ == kOutRingCapacity) {
            out_head_ = (out_head_ + 1) % kOutRingCapacity;
            --out_size_;
            stats_.output_overruns.fetch_add(1, std::memory_order_relaxed);
        }
        out_ring_[(out_head_ + out_size_) % kOutRingCapacity] = data[i];
        ++out_size_;
    }
}

void StreamingRvc::push_output_zeros(size_t n)
{
    std::vector<float> zeros(n, 0.0f);
    push_output(zeros.data(), n);
}

} // namespace rvc
