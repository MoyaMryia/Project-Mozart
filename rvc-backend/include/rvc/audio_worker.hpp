#pragma once

#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "mozart/audio_io.h"
#include "rvc/pipeline.hpp"
#include "rvc/streaming_pipeline.hpp"

namespace rvc {

// Business-layer facade between algorithm-agnostic IO and the RVC pipeline.
// It owns no socket or device resources: status_manager can open/close the
// supplied stream independently and start/stop this worker around it.
//
// 双模式：
//   - mock pipeline → 逐帧 1:1 直通（20ms 帧，零附加延迟）
//   - real pipeline → 流式滑动窗口（2s 窗分块推理 + 50ms 交叉淡化，
//                     独立推理线程，泵线程只做 IO）
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

    struct VadStats {
        uint64_t frame_count = 0;
        uint64_t voiced_frame_count = 0;
        double confidence_percent = 0.0;
    };

    struct StreamStats {
        uint64_t blocks = 0;
        uint64_t skipped_blocks = 0;
        uint64_t resets = 0;
        uint64_t late_blocks = 0;
        uint64_t input_overruns = 0;
        uint64_t output_overruns = 0;
        uint64_t inference_errors = 0;
        uint64_t output_underruns = 0;
    };

    AudioWorker(mozart_stream_handle_t stream,
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
    VadStats get_vad_stats() const;
    StreamStats get_stream_stats() const;
    bool is_stream_mode() const noexcept { return stream_mode_; }
    const Config& config() const noexcept { return config_; }

private:
    mozart_stream_handle_t stream_ = nullptr;
    RVCPipelineBase& pipeline_;
    Config config_;

    bool stream_mode_ = false;
    std::unique_ptr<StreamingRvc> streaming_;

    std::atomic<bool> running_{false};
    std::thread worker_thread_;
    std::thread inference_thread_;

    mutable std::mutex stats_mutex_;
    uint64_t latency_count_{0};
    double latency_total_ms_{0.0};
    double latency_max_ms_{0.0};
    uint64_t inference_count_{0};
    uint64_t bypass_count_{0};
    uint64_t underrun_count_{0};
    uint64_t vad_frame_count_{0};
    uint64_t voiced_frame_count_{0};
    uint64_t vad_confidence_total_{0};

    void process_loop();
    void process_frame(const mozart_input_frame_t& input,
                       mozart_output_frame_t& output);
    void record_input_meta(const mozart_input_frame_t& input);
};

} // namespace rvc
