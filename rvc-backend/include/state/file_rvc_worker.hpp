#pragma once

#include <atomic>
#include <filesystem>
#include <functional>
#include <string>

#include "mozart/frame_meta.h"
#include "rvc/pipeline.hpp"

namespace rvc {

// Offline data-plane worker. It owns decode, preprocessing, RVC inference, and
// atomic output publication for exactly one job; ModeController only schedules it.
class FileRvcWorker {
public:
    struct Config {
        std::filesystem::path storage_dir;
        std::string ffmpeg_path = "ffmpeg";
        uint32_t output_sample_rate = MOZART_OUTPUT_SAMPLE_RATE;
        bool rnnoise = false;
    };

    struct Request {
        std::string id;
        std::string model_id;
        std::filesystem::path source_path;
        std::filesystem::path output_path;
    };

    using ProgressCallback = std::function<void(int)>;

    FileRvcWorker(RVCPipelineBase& pipeline, Config config);
    bool process(const Request& request, const std::atomic<bool>& cancelled,
                 const ProgressCallback& progress, std::string& error);

private:
    RVCPipelineBase& pipeline_;
    Config config_;
};

} // namespace rvc
