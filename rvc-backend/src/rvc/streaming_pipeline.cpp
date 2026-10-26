// streaming_pipeline.cpp — 实时滑动窗口流式 RVC 实现（见头文件说明）
#include "rvc/streaming_pipeline.hpp"
#include "rvc/profile.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <utility>

#include <spdlog/spdlog.h>
#include <chrono>

namespace rvc {

namespace {
constexpr uint64_t kPtsResetGapNs = 200000000ull; // pts 跳变 >200ms 视为不连续
constexpr uint32_t kFrameIdxResetGap = 4;        // frame_idx 缺口 >4 帧视为不连续
constexpr size_t kOutRingCapacity = 96000 * 8;   // 16s @48k（推理可领先消费端数块）
constexpr float kPi = 3.14159265358979323846f;
constexpr size_t kModelFrame16k = 160;
constexpr size_t kModelFrame48k = 480;
} // namespace

StreamingRvc::StreamingRvc(Config config)
    : cfg_(std::move(config)),
      hop_in_(cfg_.upstream_realtime
          ? cfg_.window_samples
          : cfg_.window_samples - cfg_.crossfade_out / 3),
      emit_out_(hop_in_ * 3),
      out_block_(cfg_.upstream_realtime
          ? emit_out_ + cfg_.crossfade_out + cfg_.sola_search_out
          : cfg_.window_samples * 3),
      analysis_samples_(cfg_.upstream_realtime
          ? cfg_.past_context_samples + cfg_.window_samples
              + (cfg_.crossfade_out + cfg_.sola_search_out) / 3
          : cfg_.window_samples)
{
    if (cfg_.window_samples < 200 || cfg_.crossfade_out % 3 != 0
        || cfg_.sola_search_out % 3 != 0
        || cfg_.crossfade_out >= cfg_.window_samples * 3
        || (cfg_.upstream_realtime && cfg_.full_history)
        || (cfg_.upstream_realtime
            && (cfg_.past_context_samples % kModelFrame16k != 0
                || analysis_samples_ % kModelFrame16k != 0
                || out_block_ % kModelFrame48k != 0))
        || cfg_.startup_buffer_blocks == 0
        || cfg_.startup_buffer_blocks > kOutRingCapacity / emit_out_
        || cfg_.startup_delay_out % MOZART_OUTPUT_SAMPLES != 0
        || cfg_.startup_delay_out > kOutRingCapacity - cfg_.startup_buffer_blocks * emit_out_) {
        throw std::invalid_argument("StreamingRvc: invalid window/crossfade config");
    }
    ring_.resize(cfg_.full_history
        ? cfg_.max_history_samples
        : std::max(cfg_.window_samples * 4, analysis_samples_ + 2 * hop_in_));
    out_ring_.resize(kOutRingCapacity);
    tail_.assign(cfg_.crossfade_out, 0.0f);
    startup_delay_remaining_ = cfg_.startup_delay_out;
}

void StreamingRvc::handle_discontinuity()
{
    std::lock_guard<std::mutex> lk(cv_mutex_);
    std::lock_guard<std::mutex> ring_lk(ring_mutex_);
    ring_head_ = ring_size_ = 0;
    pushed_total_ = 0;
    next_window_start_ = 0;
    have_window_base_ = false;
    source_frames_.clear();
    // tail_/tail_valid_ 归推理线程所有：它检测到代数变化后自行作废
    ++reset_gen_;
    stats_.resets.fetch_add(1, std::memory_order_relaxed);
    {
        std::lock_guard<std::mutex> out_lk(out_mutex_);
        out_head_ = out_size_ = 0;
        output_spans_.clear();
        output_started_ = false;
        startup_delay_remaining_ = cfg_.startup_delay_out;
    }
}

void StreamingRvc::reset_full_history_locked()
{
    // 调用者持 cv_mutex_。full-history 窗口超出输入环容量时丢弃旧前缀并
    // 从当前位置重新锚定；否则 read_prefix 将永久失败、流永久停摆。
    {
        std::lock_guard<std::mutex> ring_lk(ring_mutex_);
        ring_head_ = ring_size_ = 0;
    }
    pushed_total_ = 0;
    owned_ = 0;
    next_window_start_ = 0;
    have_window_base_ = false;
    source_frames_.clear();
    std::fill(tail_.begin(), tail_.end(), 0.0f);
    tail_valid_ = false;
    {
        std::lock_guard<std::mutex> out_lk(out_mutex_);
        out_head_ = out_size_ = 0;
        output_spans_.clear();
        output_started_ = false;
        startup_delay_remaining_ = cfg_.startup_delay_out;
    }
    ++reset_gen_;
    stats_.history_resets.fetch_add(1, std::memory_order_relaxed);
    static std::atomic<uint64_t> logged{0};
    if (logged.fetch_add(1) < 3) {
        spdlog::warn("[stream] full-history capacity exceeded; re-anchoring stream");
    }
}

void StreamingRvc::push(const mozart_input_frame_t& frame)
{
    // ---- 不连续检测（push 侧独占状态）----
    if (have_last_meta_) {
        const uint32_t idx_gap = frame.meta.frame_idx - last_frame_idx_; // u32 回绕安全
        const bool idx_gap_bad = idx_gap == 0 || idx_gap > kFrameIdxResetGap;
        const bool pts_jump = frame.meta.pts_ns < last_pts_ns_
            || frame.meta.pts_ns - last_pts_ns_ > kPtsResetGapNs;
        // ASR utterance boundaries do not interrupt the PCM timeline.
        if (idx_gap_bad || pts_jump) {
            handle_discontinuity();
        }
    }
    last_frame_idx_ = frame.meta.frame_idx;
    last_pts_ns_ = frame.meta.pts_ns;
    have_last_meta_ = true;

    // 样本、元数据和累计位置在同一次取窗快照中发布。
    std::lock_guard<std::mutex> schedule_lock(cv_mutex_);
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

    source_frames_.push_back({pushed_total_, frame.meta});
    pushed_total_ += MOZART_INPUT_SAMPLES;
    const uint64_t oldest = pushed_total_ > ring_.size() ? pushed_total_ - ring_.size() : 0;
    while (!source_frames_.empty()
           && source_frames_.front().sample_start + MOZART_INPUT_SAMPLES <= oldest) {
        source_frames_.pop_front();
    }
    cv_.notify_one();
}

void StreamingRvc::read_last_window(size_t count, std::vector<float>& dst)
{
    std::lock_guard<std::mutex> lk(ring_mutex_);
    dst.assign(count, 0.0f);
    const size_t take = std::min(ring_size_, count);
    const size_t dst_off = count - take;
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
    uint64_t window_gen = 0;
    int64_t output_start = 0;
    std::vector<SourceFrame> sources;

    {
        std::unique_lock<std::mutex> lk(cv_mutex_);
        window_gen = reset_gen_.load(std::memory_order_relaxed);

        // 等待窗口就绪（try_process_one 语义：不等待，仅检查）
        if (cfg_.full_history) {
            const uint64_t required = next_window_start_ + cfg_.window_samples
                + cfg_.right_context_samples + cfg_.guard_samples;
            // 先处理超容：历史前缀已被环丢弃时永远等不到 read_prefix。
            if (required > cfg_.max_history_samples) {
                reset_full_history_locked();
                return false;
            }
            if (pushed_total_ < required) {
                return false;
            }
        } else if (have_window_base_) {
            if (pushed_total_ - owned_ < hop_in_) return false;
        } else {
            const size_t startup_samples = cfg_.upstream_realtime
                ? hop_in_ : cfg_.window_samples;
            if (pushed_total_ < startup_samples) return false;
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
            if (!read_prefix(inference_end, window)) {
                // 环已回绕、前缀被丢弃：重新锚定而不是永久失败
                reset_full_history_locked();
                return false;
            }
            next_window_start_ += hop_in_;
        } else {
            read_last_window(analysis_samples_, window);
        }
        output_start = cfg_.full_history ? static_cast<int64_t>(window_start * 3)
            : static_cast<int64_t>(pushed_total_ * 3)
                - static_cast<int64_t>(cfg_.upstream_realtime ? out_block_ : cfg_.window_samples * 3);
        for (const auto& source : source_frames_) {
            const int64_t begin = static_cast<int64_t>(source.sample_start * 3);
            if (begin + MOZART_OUTPUT_SAMPLES <= output_start) continue;
            if (begin >= output_start + static_cast<int64_t>(out_block_)) break;
            sources.push_back(source);
            voiced = voiced || source.meta.vad_flag != 0;
        }
        owned_ = pushed_total_;
        pushed_at_window = pushed_total_;
        have_window_base_ = true;
    }

    if (tail_gen_ != window_gen) {
        tail_gen_ = window_gen;
        std::fill(tail_.begin(), tail_.end(), 0.0f);
        tail_valid_ = false;
    }

    // ---- 静音窗：跳过推理，输出等长静音 ----
    if (cfg_.skip_silence && !voiced) {
        static std::atomic<uint64_t> skip_logged{0};
        if (skip_logged.fetch_add(1) < 3) {
            spdlog::warn("[stream] window skipped as silent (pushed={})", pushed_at_window);
        }
        std::fill(tail_.begin(), tail_.end(), 0.0f);
        tail_valid_ = false;
        push_output_zeros(window_gen, sources, output_start);
        stats_.skipped_blocks.fetch_add(1, std::memory_order_relaxed);
        return true;
    }

    // ---- 推理（耗时操作，锁外执行）----
    std::vector<float> converted;
    const auto t_infer = std::chrono::steady_clock::now();
    try {
        if (cfg_.upstream_realtime) {
            const RvcRealtimeRequest request{
                cfg_.window_samples,
                cfg_.past_context_samples / kModelFrame16k,
                out_block_ / kModelFrame48k,
                out_block_
            };
            converted = pipeline.process_realtime(window, request);
        } else {
            converted = pipeline.process(window);
        }
    } catch (const std::exception& e) {
        stats_.inference_errors.fetch_add(1, std::memory_order_relaxed);
        static std::atomic<uint64_t> logged{0};
        if (logged.fetch_add(1) < 5) {
            spdlog::warn("streaming inference failed: {}", e.what());
        }
        std::fill(tail_.begin(), tail_.end(), 0.0f);
        tail_valid_ = false;
        push_output_zeros(window_gen, sources, output_start);
        return true;
    }
    const auto t_pipeline_done = profile::Clock::now();

    if (cfg_.full_history) {
        const size_t output_start = static_cast<size_t>(window_start) * 3;
        if (converted.size() < output_start + out_block_) {
            stats_.inference_errors.fetch_add(1, std::memory_order_relaxed);
            spdlog::warn(
                "quality streaming: prefix returned {} samples, need at least {}",
                converted.size(), output_start + out_block_);
            std::fill(tail_.begin(), tail_.end(), 0.0f);
            tail_valid_ = false;
            push_output_zeros(window_gen, sources, output_start);
            return true;
        }
        converted = std::vector<float>(
            converted.begin() + static_cast<std::ptrdiff_t>(output_start),
            converted.begin() + static_cast<std::ptrdiff_t>(output_start + out_block_));
    }
    const auto t_crop_done = profile::Clock::now();

    if (converted.size() < out_block_) {
        stats_.inference_errors.fetch_add(1, std::memory_order_relaxed);
        spdlog::warn("streaming: generator returned {} samples, expect {}",
                     converted.size(), out_block_);
        std::fill(tail_.begin(), tail_.end(), 0.0f);
        tail_valid_ = false;
        push_output_zeros(window_gen, sources, output_start);
        return true;
    }

    // 推理期间发生不连续重置：本块属于旧流，丢弃（重置路径已清空输出环）
    {
        std::lock_guard<std::mutex> lk(cv_mutex_);
        if (reset_gen_.load(std::memory_order_relaxed) != window_gen) {
            return true;
        }
    }

    // ---- 块间对齐和拼接 ----
    if (stale_tail) {
        std::fill(tail_.begin(), tail_.end(), 0.0f);
        tail_valid_ = false;
    }
    const size_t cf = cfg_.crossfade_out;
    std::vector<float> emit;
    emit.resize(emit_out_);
    std::vector<OutputSpan> overlap_metadata;

    if (cfg_.upstream_realtime) {
        if (tail_valid_) overlap_metadata = output_metadata(tail_sources_, tail_output_start_, cf);
        size_t sola_offset = 0;
        if (tail_valid_) {
            double best_score = -std::numeric_limits<double>::infinity();
            for (size_t offset = 0; offset <= cfg_.sola_search_out; ++offset) {
                double numerator = 0.0;
                double energy = 1e-8;
                for (size_t i = 0; i < cf; ++i) {
                    const double sample = converted[offset + i];
                    numerator += static_cast<double>(tail_[i]) * sample;
                    energy += sample * sample;
                }
                const double score = numerator / std::sqrt(energy);
                if (score > best_score) {
                    best_score = score;
                    sola_offset = offset;
                }
            }
        }
        if (converted.size() < sola_offset + emit_out_ + cf) {
            stats_.inference_errors.fetch_add(1, std::memory_order_relaxed);
            spdlog::warn("realtime SOLA: output too short after offset {}", sola_offset);
            std::fill(tail_.begin(), tail_.end(), 0.0f);
            tail_valid_ = false;
            push_output_zeros(window_gen, sources, output_start);
            return true;
        }
        output_start += static_cast<int64_t>(sola_offset);
        std::copy_n(
            converted.begin() + static_cast<std::ptrdiff_t>(sola_offset),
            emit_out_, emit.begin()
        );
        for (size_t i = 0; i < cf; ++i) {
            const float position = cf > 1
                ? static_cast<float>(i) / static_cast<float>(cf - 1) : 1.0f;
            const float fade_in = std::sin(0.5f * kPi * position);
            const float weight = fade_in * fade_in;
            emit[i] = emit[i] * weight + tail_[i] * (1.0f - weight);
        }
        std::copy_n(
            converted.begin() + static_cast<std::ptrdiff_t>(sola_offset + emit_out_),
            cf, tail_.begin()
        );
        tail_sources_ = sources;
        tail_output_start_ = output_start + static_cast<int64_t>(emit_out_);
        tail_valid_ = true;
    } else if (!tail_valid_) {
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
    if (!cfg_.upstream_realtime) {
        // 新淡化尾 = 本块最后 cf 个样本（与下一块前 cf 对应同一段输入）
        std::copy_n(converted.begin() + static_cast<std::ptrdiff_t>(out_block_ - cf),
                    cf, tail_.begin());
        tail_valid_ = true;
    }

    auto metadata = overlap_metadata.empty()
        ? output_metadata(sources, output_start, emit.size()) : std::move(overlap_metadata);
    size_t prefix = 0;
    for (const auto& span : metadata) prefix += span.samples;
    if (prefix < emit.size()) {
        const auto suffix = output_metadata(sources, output_start + static_cast<int64_t>(prefix), emit.size() - prefix);
        metadata.insert(metadata.end(), suffix.begin(), suffix.end());
    }
    if (!push_output(emit.data(), emit.size(), window_gen, metadata)) return true;
    stats_.blocks.fetch_add(1, std::memory_order_relaxed);
    if (profile::enabled()) {
        const auto t_done = profile::Clock::now();
        spdlog::info(
            "[profile][stream] window_start={} prefix={} pipeline={:.3f}ms crop={:.3f}ms "
            "crossfade_ring={:.3f}ms total={:.3f}ms",
            window_start, window.size(), profile::elapsed_ms(t_infer, t_pipeline_done),
            profile::elapsed_ms(t_pipeline_done, t_crop_done),
            profile::elapsed_ms(t_crop_done, t_done), profile::elapsed_ms(t_infer, t_done));
    }
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
        "full_history={} upstream_realtime={} right_context={} guard={} analysis={}",
        cfg_.window_samples, hop_in_, cfg_.crossfade_out, cfg_.full_history,
        cfg_.upstream_realtime, cfg_.right_context_samples, cfg_.guard_samples,
        analysis_samples_);
    uint64_t seen_reset = reset_gen_.load();
    while (running.load(std::memory_order_relaxed)) {
        {
            std::unique_lock<std::mutex> lk(cv_mutex_);
            if (reset_gen_.load() != seen_reset) {
                seen_reset = reset_gen_.load();
                std::fill(tail_.begin(), tail_.end(), 0.0f);
                tail_valid_ = false;
                if (cfg_.upstream_realtime) pipeline.reset_realtime();
                continue;
            }
            const uint64_t required = next_window_start_ + cfg_.window_samples
                + cfg_.right_context_samples + cfg_.guard_samples;
            // full-history 超容不再让 readiness 永久为假：try_process_one
            // 会在超容时重新锚定并继续出块。
            const bool ready = cfg_.full_history
                ? pushed_total_ >= required
                : (have_window_base_
                    ? pushed_total_ - owned_ >= hop_in_
                    : pushed_total_ >= (cfg_.upstream_realtime
                        ? hop_in_ : cfg_.window_samples));
            if (!ready) {
                cv_.wait_for(lk, std::chrono::milliseconds(50));
                continue;
            }
        }
        if (reset_gen_.load() != seen_reset) {
            seen_reset = reset_gen_.load();
            if (cfg_.upstream_realtime) pipeline.reset_realtime();
            continue;
        }
        try_process_one(pipeline);
    }
    spdlog::info("StreamingRvc inference thread stopped: blocks={} skipped={} resets={}",
                 stats_.blocks.load(), stats_.skipped_blocks.load(),
                 stats_.resets.load());
}

size_t StreamingRvc::pop_output(float* dst, size_t n, mozart_frame_meta_t* source_meta)
{
    if (source_meta) *source_meta = {};
    if (n == 0) return 0;
    std::lock_guard<std::mutex> lk(out_mutex_);
    if (!output_started_) {
        if (startup_delay_remaining_ > 0) {
            startup_delay_remaining_ -= std::min(n, startup_delay_remaining_);
            return 0;
        }
        if (out_size_ < cfg_.startup_buffer_blocks * emit_out_) return 0;
        output_started_ = true;
    }
    const size_t got = std::min(n, out_size_);
    if (got && source_meta) {
        const auto& span = output_spans_.front();
        *source_meta = span.meta;
        if (source_meta->pts_ns) {
            source_meta->pts_ns += span.consumed * 1000000000ull / MOZART_OUTPUT_SAMPLE_RATE;
        }
    }
    for (size_t i = 0; i < got; ++i) {
        dst[i] = out_ring_[out_head_];
        out_head_ = (out_head_ + 1) % kOutRingCapacity;
    }
    out_size_ -= got;
    consume_output_meta(got);
    return got;
}

size_t StreamingRvc::output_pending() const
{
    std::lock_guard<std::mutex> lk(out_mutex_);
    return out_size_;
}

void StreamingRvc::consume_output_meta(size_t n)
{
    while (n) {
        auto& span = output_spans_.front();
        const size_t take = std::min(n, span.samples);
        span.samples -= take;
        span.consumed += take;
        n -= take;
        if (!span.samples) output_spans_.pop_front();
    }
}

std::vector<StreamingRvc::OutputSpan> StreamingRvc::output_metadata(
    const std::vector<SourceFrame>& sources, int64_t output_start, size_t n)
{
    std::vector<OutputSpan> metadata;
    const int64_t end = output_start + static_cast<int64_t>(n);
    int64_t at = output_start;
    for (const auto& source : sources) {
        const int64_t begin = static_cast<int64_t>(source.sample_start * 3);
        const int64_t stop = std::min(end, begin + MOZART_OUTPUT_SAMPLES);
        if (stop <= at) continue;
        if (begin >= end) break;
        if (at < begin) {
            metadata.push_back({static_cast<size_t>(begin - at), 0, {}});
            at = begin;
        }
        auto meta = source.meta;
        if (meta.pts_ns) meta.pts_ns += static_cast<uint64_t>(at - begin) * 1000000000ull / MOZART_OUTPUT_SAMPLE_RATE;
        metadata.push_back({static_cast<size_t>(stop - at), 0, meta});
        at = stop;
    }
    if (at < end) metadata.push_back({static_cast<size_t>(end - at), 0, {}});
    return metadata;
}

bool StreamingRvc::push_output(const float* data, size_t n, uint64_t generation,
                               const std::vector<OutputSpan>& metadata)
{
    std::lock_guard<std::mutex> lk(out_mutex_);
    // reset 先推进代数再清空输出；此锁将代数检查与写入放在同一事务中。
    if (reset_gen_.load() != generation) return false;
    const size_t discard = out_size_ + n > kOutRingCapacity ? out_size_ + n - kOutRingCapacity : 0;
    consume_output_meta(discard);
    out_head_ = (out_head_ + discard) % kOutRingCapacity;
    out_size_ -= discard;
    stats_.output_overruns.fetch_add(discard, std::memory_order_relaxed);

    output_spans_.insert(output_spans_.end(), metadata.begin(), metadata.end());
    const size_t write_at = (out_head_ + out_size_) % kOutRingCapacity;
    const size_t first = std::min(n, kOutRingCapacity - write_at);
    std::copy_n(data, first, out_ring_.begin() + static_cast<std::ptrdiff_t>(write_at));
    std::copy_n(data + first, n - first, out_ring_.begin());
    out_size_ += n;
    return true;
}

void StreamingRvc::push_output_zeros(uint64_t generation, const std::vector<SourceFrame>& sources,
                                     int64_t output_start)
{
    std::vector<float> zeros(emit_out_, 0.0f);
    push_output(zeros.data(), zeros.size(), generation, output_metadata(sources, output_start, zeros.size()));
}

} // namespace rvc
