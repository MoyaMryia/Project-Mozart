#pragma once

#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "mozart/audio_stream.hpp"
#include "rvc/pipeline.hpp"

namespace rvc {

// Business-layer facade between algorithm-agnostic IO and the RVC pipeline.
// It owns no socket or device resources: status_manager can open/close the
// supplied stream independently and start/stop this worker around it.
class AudioWorker {
public:
    struct Config {
        std::string host;
        uint16_t port = 0;
        uint32_t input_sample_rate = MOZART_INPUT_SAMPLE_RATE;
        uint32_t output_sample_rate = MOZART_OUTPUT_SAMPLE_RATE;
        uint32_t frame_duration_ms = MOZART_INPUT_FRAME_MS;
        bool skip_silence = true;
    };

    struct LatencyStats {
        uint64_t count = 0;
        double avg_ms = 0.0;
        double max_ms = 0.0;
    };

    struct BypassStats {
        uint64_t inference_count = 0;
        uint64_t bypass_count = 0;
    };

    AudioWorker(mozart::RealTimeAudioStream& stream,
                RVCPipelineBase& pipeline,
                Config config);
    ~AudioWorker();

    AudioWorker(const AudioWorker&) = delete;
    AudioWorker& operator=(const AudioWorker&) = delete;

    void start();
    void stop();
    bool running() const noexcept { return running_.load(); }

    LatencyStats get_latency_stats() const;
    BypassStats get_bypass_stats() const;
    const Config& config() const noexcept { return config_; }

private:
    mozart::RealTimeAudioStream& stream_;
    RVCPipelineBase& pipeline_;
    Config config_;

    std::atomic<bool> running_{false};
    std::thread worker_thread_;

    mutable std::mutex stats_mutex_;
    uint64_t latency_count_{0};
    double latency_total_ms_{0.0};
    double latency_max_ms_{0.0};
    uint64_t inference_count_{0};
    uint64_t bypass_count_{0};

    void process_loop();
    void process_frame(const mozart_input_frame_t& input,
                       mozart_output_frame_t& output);
};

} // namespace rvc
