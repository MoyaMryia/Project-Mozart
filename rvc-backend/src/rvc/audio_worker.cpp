#include "rvc/audio_worker.hpp"

#include <algorithm>
#include <chrono>
#include <cstring>
#include <stdexcept>
#include <utility>

#include <spdlog/spdlog.h>

namespace rvc {

namespace {
// pts 可信（非离线合成 0 值）时用于端到端延迟统计
constexpr uint64_t kMinPlausiblePtsNs = 1000000000ull; // >1s 说明是真时钟
} // namespace

AudioWorker::AudioWorker(mozart_stream_handle_t stream,
                         RVCPipelineBase& pipeline,
                         Config config)
    : stream_(stream), pipeline_(pipeline), config_(std::move(config))
{}

AudioWorker::~AudioWorker() {
    stop();
}

void AudioWorker::start() {
    if (running_.exchange(true)) return;
    if (worker_thread_.joinable()) worker_thread_.join();
    if (!mozart_io_is_stream_open(stream_)) {
        running_ = false;
        throw std::runtime_error("audio stream must be open before AudioWorker::start");
    }
    if (config_.input_sample_rate != MOZART_INPUT_SAMPLE_RATE
        || config_.output_sample_rate != MOZART_OUTPUT_SAMPLE_RATE
        || config_.frame_duration_ms != MOZART_INPUT_FRAME_MS) {
        running_ = false;
        throw std::invalid_argument("AudioWorker config does not match the fixed IO contract");
    }

    stream_mode_ = !pipeline_.is_mock();
    if (stream_mode_) {
        StreamingRvc::Config scfg;
        scfg.skip_silence = config_.skip_silence;
        scfg.full_history = pipeline_.supports_quality_streaming();
        if (scfg.full_history) {
            scfg.right_context_samples = 32000;
            scfg.guard_samples = 80;
            scfg.startup_buffer_blocks = 3;
        }
        streaming_ = std::make_unique<StreamingRvc>(scfg);
        inference_thread_ = std::thread([this] {
            streaming_->inference_loop(pipeline_, running_);
        });
        spdlog::info("AudioWorker stream mode: {}",
            scfg.full_history
                ? "Golden quality profile (full history + 2s lookahead + 2 hop reserve)"
                : "sliding window (2s) + 60ms crossfade");
    } else {
        spdlog::info("AudioWorker frame mode (mock pipeline)");
    }
    worker_thread_ = std::thread(&AudioWorker::process_loop, this);
}

void AudioWorker::stop() {
    const bool was_running = running_.exchange(false);
    // Closing the stream releases a blocking ReadFrame during mode changes.
    if (was_running && mozart_io_is_stream_open(stream_)) mozart_io_close_stream(stream_);
    if (inference_thread_.joinable()) inference_thread_.join();
    if (worker_thread_.joinable()) worker_thread_.join();
    streaming_.reset();
}

void AudioWorker::record_input_meta(const mozart_input_frame_t& input)
{
    std::lock_guard<std::mutex> lock(stats_mutex_);
    ++vad_frame_count_;
    voiced_frame_count_ += input.meta.vad_flag != 0;
    vad_confidence_total_ += input.meta.conf;
}

void AudioWorker::process_loop() {
    spdlog::info("AudioWorker started: {}Hz -> {}Hz, frame={}ms",
                 config_.input_sample_rate,
                 config_.output_sample_rate,
                 config_.frame_duration_ms);

    while (running_.load()) {
        mozart_input_frame_t input{};
        if (!mozart_io_read_frame(stream_, &input, sizeof(input))) {
            if (!running_.load() || !mozart_io_is_stream_open(stream_)) break;
            continue;
        }
        record_input_meta(input);

        const auto started = std::chrono::steady_clock::now();
        mozart_output_frame_t output{};
        if (stream_mode_) {
            output.meta = input.meta;
            streaming_->push(input);
            const size_t got = streaming_->pop_output(
                output.pcm, MOZART_OUTPUT_SAMPLES);
            if (got < MOZART_OUTPUT_SAMPLES) {
                std::fill(output.pcm + got, output.pcm + MOZART_OUTPUT_SAMPLES, 0.0f);
            }
            std::lock_guard<std::mutex> lock(stats_mutex_);
            if (got < MOZART_OUTPUT_SAMPLES) {
                ++underrun_count_;
                if (!stream_output_started_) ++startup_underrun_count_;
            }
            if (got > 0) stream_output_started_ = true;
        } else {
            process_frame(input, output);
        }
        if (!mozart_io_write_frame(stream_, &output, sizeof(output)) && running_.load()) {
            spdlog::warn("AudioWorker failed to write output frame {}",
                         output.meta.frame_idx);
        }

        // 流式模式优先用 pts 算真实端到端延迟；否则退化为泵耗时
        double measured_ms;
        if (stream_mode_ && input.meta.pts_ns > kMinPlausiblePtsNs) {
            const auto now_ns = static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(
                    std::chrono::steady_clock::now().time_since_epoch()).count());
            measured_ms = static_cast<double>(now_ns - input.meta.pts_ns) / 1e6;
        } else {
            measured_ms = std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - started).count();
        }
        std::lock_guard<std::mutex> lock(stats_mutex_);
        ++latency_count_;
        latency_total_ms_ += measured_ms;
        latency_max_ms_ = std::max(latency_max_ms_, measured_ms);
    }

    running_ = false;
    spdlog::info("AudioWorker stopped");
}


void AudioWorker::process_frame(const mozart_input_frame_t& input,
                                mozart_output_frame_t& output) {
    output.meta = input.meta;

    if (config_.skip_silence && input.meta.vad_flag == 0) {
        std::memset(output.pcm, 0, sizeof(output.pcm));
        std::lock_guard<std::mutex> lock(stats_mutex_);
        ++bypass_count_;
        return;
    }

    std::vector<float> samples(input.pcm, input.pcm + MOZART_INPUT_SAMPLES);
    std::vector<float> converted = pipeline_.process(samples);
    const size_t copy_count = std::min(converted.size(),
                                       static_cast<size_t>(MOZART_OUTPUT_SAMPLES));
    std::copy_n(converted.begin(), copy_count, output.pcm);
    if (copy_count < MOZART_OUTPUT_SAMPLES) {
        std::fill(output.pcm + copy_count,
                  output.pcm + MOZART_OUTPUT_SAMPLES,
                  0.0f);
    }

    std::lock_guard<std::mutex> lock(stats_mutex_);
    ++inference_count_;
}

AudioWorker::LatencyStats AudioWorker::get_latency_stats() const {
    std::lock_guard<std::mutex> lock(stats_mutex_);
    LatencyStats stats;
    stats.count = latency_count_;
    stats.avg_ms = latency_count_ == 0 ? 0.0 : latency_total_ms_ / latency_count_;
    stats.max_ms = latency_max_ms_;
    return stats;
}

AudioWorker::BypassStats AudioWorker::get_bypass_stats() const {
    std::lock_guard<std::mutex> lock(stats_mutex_);
    return {inference_count_, bypass_count_};
}

AudioWorker::VadStats AudioWorker::get_vad_stats() const {
    std::lock_guard<std::mutex> lock(stats_mutex_);
    return {vad_frame_count_, voiced_frame_count_,
            vad_frame_count_ == 0 ? 0.0 : 100.0 * vad_confidence_total_ / (255.0 * vad_frame_count_)};
}

AudioWorker::StreamStats AudioWorker::get_stream_stats() const {
    StreamStats s;
    if (streaming_) {
        s.blocks = streaming_->stats().blocks.load();
        s.skipped_blocks = streaming_->stats().skipped_blocks.load();
        s.resets = streaming_->stats().resets.load();
        s.late_blocks = streaming_->stats().late_blocks.load();
        s.input_overruns = streaming_->stats().input_overruns.load();
        s.output_overruns = streaming_->stats().output_overruns.load();
        s.inference_errors = streaming_->stats().inference_errors.load();
    }
    std::lock_guard<std::mutex> lock(stats_mutex_);
    s.output_underruns = underrun_count_;
    s.startup_output_underruns = startup_underrun_count_;
    return s;
}

} // namespace rvc
