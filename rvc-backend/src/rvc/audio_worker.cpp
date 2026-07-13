#include "rvc/audio_worker.hpp"

#include <algorithm>
#include <chrono>
#include <cstring>
#include <stdexcept>
#include <utility>

#include <spdlog/spdlog.h>

namespace rvc {

AudioWorker::AudioWorker(mozart::RealTimeAudioStream& stream,
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
    if (!stream_.IsOpen()) {
        running_ = false;
        throw std::runtime_error("audio stream must be open before AudioWorker::start");
    }
    if (config_.input_sample_rate != MOZART_INPUT_SAMPLE_RATE
        || config_.output_sample_rate != MOZART_OUTPUT_SAMPLE_RATE
        || config_.frame_duration_ms != MOZART_INPUT_FRAME_MS) {
        running_ = false;
        throw std::invalid_argument("AudioWorker config does not match the fixed IO contract");
    }
    worker_thread_ = std::thread(&AudioWorker::process_loop, this);
}

void AudioWorker::stop() {
    const bool was_running = running_.exchange(false);
    // Closing the stream releases a blocking ReadFrame during mode changes.
    if (was_running && stream_.IsOpen()) stream_.Close();
    if (worker_thread_.joinable()) worker_thread_.join();
}

void AudioWorker::process_loop() {
    spdlog::info("AudioWorker started: {}Hz -> {}Hz, frame={}ms",
                 config_.input_sample_rate,
                 config_.output_sample_rate,
                 config_.frame_duration_ms);

    while (running_.load()) {
        mozart_input_frame_t input{};
        if (!stream_.ReadFrame(&input, sizeof(input))) {
            if (!running_.load() || !stream_.IsOpen()) break;
            continue;
        }

        const auto started = std::chrono::steady_clock::now();
        mozart_output_frame_t output{};
        process_frame(input, output);
        if (!stream_.WriteFrame(&output, sizeof(output)) && running_.load()) {
            spdlog::warn("AudioWorker failed to write output frame {}",
                         output.meta.frame_idx);
        }

        const double elapsed_ms = std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - started).count();
        std::lock_guard<std::mutex> lock(stats_mutex_);
        ++latency_count_;
        latency_total_ms_ += elapsed_ms;
        latency_max_ms_ = std::max(latency_max_ms_, elapsed_ms);
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

} // namespace rvc
