#include "mozart/state_daemon.hpp"

#include <filesystem>
#include <optional>
#include <utility>

#include <spdlog/spdlog.h>

#include "mozart/http_api.hpp"
#include "rvc/pipeline.hpp"
#include "state/mode_controller.hpp"
#include "utils/config.hpp"

namespace mozart {

StateManagerDaemon::StateManagerDaemon(rvc::Config config)
    : config_(std::move(config)) {}

StateManagerDaemon::~StateManagerDaemon() {
    stop();
}

bool StateManagerDaemon::start() {
    if (started_) return true;

    const uint32_t input_rate = config_.get_int("input.contract.sample_rate", MOZART_INPUT_SAMPLE_RATE);
    const uint32_t output_rate = config_.get_int("output.sample_rate", MOZART_OUTPUT_SAMPLE_RATE);
    const uint32_t frame_ms = config_.get_int("network.audio.frame_duration_ms", MOZART_INPUT_FRAME_MS);
    const rvc::RvcMockConfig mock{
        config_.get_bool("rvc.mock.generator", false),
        config_.get_bool("rvc.mock.hubert", false),
        config_.get_bool("rvc.mock.rmvpe", false)
    };
    const std::filesystem::path models_dir = config_.resolve_file_path("rvc.models_dir", "./models");
    const std::filesystem::path hubert_path = config_.resolve_file_path("rvc.hubert_path", "./assets/hubert/hubert_base.onnx");
    const std::string rmvpe_path_text = config_.get_string("rvc.rmvpe_path", "");
    const std::optional<std::filesystem::path> rmvpe_path = rmvpe_path_text.empty()
        ? std::nullopt : std::optional<std::filesystem::path>(config_.resolve_file_path("rvc.rmvpe_path", ""));
    const std::string realtime_hubert_text = config_.get_string("rvc.realtime_hubert_path", "");
    const std::optional<std::filesystem::path> realtime_hubert_path = realtime_hubert_text.empty()
        ? std::nullopt : std::optional<std::filesystem::path>(config_.resolve_file_path("rvc.realtime_hubert_path", ""));
    const std::string realtime_rmvpe_text = config_.get_string("rvc.realtime_rmvpe_path", "");
    const std::optional<std::filesystem::path> realtime_rmvpe_path = realtime_rmvpe_text.empty()
        ? std::nullopt : std::optional<std::filesystem::path>(config_.resolve_file_path("rvc.realtime_rmvpe_path", ""));
    spdlog::info("RVC config: generator={}, hubert={}, rmvpe={}, models={}, hubert_path={}, rmvpe_path={}, realtime_hubert_path={}, realtime_rmvpe_path={}",
                 mock.generator ? "mock" : "real", mock.hubert ? "mock" : "real",
                 mock.rmvpe ? "mock" : "real", models_dir.string(), hubert_path.string(),
                 rmvpe_path ? rmvpe_path->string() : "disabled",
                 realtime_hubert_path ? realtime_hubert_path->string() : "disabled",
                 realtime_rmvpe_path ? realtime_rmvpe_path->string() : "disabled");
    rvc::RvcParameters default_parameters;
    default_parameters.f0_method = config_.get_string("rvc.f0_method", default_parameters.f0_method);
    default_parameters.pitch_shift = config_.get_int("rvc.pitch_shift", default_parameters.pitch_shift);
    default_parameters.index_rate = static_cast<float>(config_.get_double("rvc.index_rate", default_parameters.index_rate));
    default_parameters.filter_radius = config_.get_int("rvc.filter_radius", default_parameters.filter_radius);
    default_parameters.rms_mix_rate = static_cast<float>(config_.get_double("rvc.rms_mix_rate", default_parameters.rms_mix_rate));
    default_parameters.protect = static_cast<float>(config_.get_double("rvc.protect", default_parameters.protect));
    pipeline_ = rvc::RVCPipelineFactory::create(
        mock, models_dir, hubert_path, rmvpe_path,
        input_rate, output_rate, config_.get_string("rvc.device", "cuda"),
        config_.get_bool("rvc.half", false), default_parameters,
        realtime_hubert_path, realtime_rmvpe_path);

    rvc::ModeController::Config controller_config;
    controller_config.audio_host = config_.get_string("network.audio.host", "0.0.0.0");
    controller_config.audio_port = static_cast<uint16_t>(config_.get_int("network.audio.port", 18000));
    controller_config.input_sample_rate = input_rate;
    controller_config.output_sample_rate = output_rate;
    controller_config.frame_duration_ms = frame_ms;
    controller_config.skip_silence = config_.get_bool("input.meta.vad_enabled", true);
    controller_config.storage_dir = config_.resolve_file_path("storage.temp_dir", "./storage/temp");
    controller_config.ffmpeg_path = config_.get_string("storage.ffmpeg_path", "ffmpeg");
    controller_config.max_queue_depth = static_cast<size_t>(config_.get_int("storage.max_queue_depth", 50));
    controller_config.max_cache_bytes = static_cast<uint64_t>(config_.get_int("storage.max_cache_size_mb", 1000)) * 1024ULL * 1024ULL;
    controller_config.models_dir = models_dir;
    controller_config.presets_path = config_.resolve_file_path("storage.presets_path", "./storage/presets.json");
    controller_config.default_parameters = default_parameters;
    controller_ = std::make_unique<rvc::ModeController>(*pipeline_, std::move(controller_config));

    api_ = std::make_unique<rvc::HttpApiServer>(
        config_.get_string("network.control.host", "0.0.0.0"),
        static_cast<uint16_t>(config_.get_int("network.control.port", 18080)),
        controller_.get());
    if (!api_->start()) {
        api_.reset();
        controller_.reset();
        pipeline_.reset();
        return false;
    }
    started_ = true;
    spdlog::info("state daemon started; initial mode is IDLE");
    return true;
}

void StateManagerDaemon::stop() {
    if (!started_) return;
    api_->stop();
    api_.reset();
    controller_.reset();
    pipeline_.reset();
    started_ = false;
}

} // namespace mozart
