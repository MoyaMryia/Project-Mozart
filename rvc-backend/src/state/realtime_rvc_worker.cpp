#include "state/realtime_rvc_worker.hpp"

#include <stdexcept>
#include <utility>

namespace rvc {

RealtimeRvcWorker::RealtimeRvcWorker(RVCPipelineBase& pipeline, Config config)
    : pipeline_(pipeline), config_(std::move(config)) {}

RealtimeRvcWorker::~RealtimeRvcWorker() {
    stop();
}

void RealtimeRvcWorker::start() {
    if (running()) return;
    stream_ = mozart_io_create_udp_stream(config_.host.c_str(), config_.port, MOZART_IO_DIR_CAPTURE);
    if (!stream_) throw std::runtime_error("failed to create UDP audio stream");
    if (!mozart_io_open_stream(stream_, config_.input_sample_rate, config_.frame_duration_ms, 16)) {
        mozart_io_destroy_stream(stream_);
        stream_ = nullptr;
        throw std::runtime_error("failed to open UDP audio stream");
    }

    AudioWorker::Config worker_config;
    worker_config.host = config_.host;
    worker_config.port = config_.port;
    worker_config.input_sample_rate = config_.input_sample_rate;
    worker_config.output_sample_rate = config_.output_sample_rate;
    worker_config.frame_duration_ms = config_.frame_duration_ms;
    worker_config.skip_silence = config_.skip_silence;
    worker_ = std::make_unique<AudioWorker>(stream_, pipeline_, worker_config);
    try {
        worker_->start();
    } catch (...) {
        worker_.reset();
        mozart_io_close_stream(stream_);
        mozart_io_destroy_stream(stream_);
        stream_ = nullptr;
        throw;
    }
}

void RealtimeRvcWorker::stop() {
    if (worker_) {
        worker_->stop();
        worker_.reset();
    }
    if (stream_) {
        mozart_io_close_stream(stream_);
        mozart_io_destroy_stream(stream_);
        stream_ = nullptr;
    }
}

bool RealtimeRvcWorker::running() const noexcept {
    return worker_ && worker_->running();
}

AudioWorker::VadStats RealtimeRvcWorker::vad_stats() const {
    return worker_ ? worker_->get_vad_stats() : AudioWorker::VadStats{};
}

} // namespace rvc
