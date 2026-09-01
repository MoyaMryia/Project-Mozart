#pragma once

#include <cstdint>
#include <memory>
#include <string>

#include "mozart/audio_io.h"
#include "rvc/audio_worker.hpp"
#include "rvc/pipeline.hpp"

namespace rvc {

// Data-plane facade for the UDP contract stream. The state manager only calls
// start/stop; it never touches stream frames or AudioWorker internals.
class RealtimeRvcWorker {
public:
    struct Config {
        std::string host;
        uint16_t port = 18000;
        uint32_t input_sample_rate = MOZART_INPUT_SAMPLE_RATE;
        uint32_t output_sample_rate = MOZART_OUTPUT_SAMPLE_RATE;
        uint32_t frame_duration_ms = MOZART_INPUT_FRAME_MS;
        bool skip_silence = true;
    };

    RealtimeRvcWorker(RVCPipelineBase& pipeline, Config config);
    ~RealtimeRvcWorker();

    RealtimeRvcWorker(const RealtimeRvcWorker&) = delete;
    RealtimeRvcWorker& operator=(const RealtimeRvcWorker&) = delete;

    void start();
    void stop();
    bool running() const noexcept;
    AudioWorker::VadStats vad_stats() const;
    AudioWorker::LatencyStats latency_stats() const;
    AudioWorker::BypassStats bypass_stats() const;
    AudioWorker::StreamStats stream_stats() const;
    bool is_stream_mode() const noexcept;

private:
    RVCPipelineBase& pipeline_;
    Config config_;
    mozart_stream_handle_t stream_ = nullptr;
    std::unique_ptr<AudioWorker> worker_;
};

} // namespace rvc
