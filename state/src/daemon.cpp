#include "mozart/state_daemon.hpp"

#include <arpa/inet.h>
#include <filesystem>
#include <limits>
#include <optional>
#include <utility>

#include <spdlog/spdlog.h>

#include "mozart/http_api.hpp"
#include "mozart/control_api.hpp"
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

    const auto fixed_integer = [&](const std::string& key, int expected) {
        return static_cast<uint32_t>(config_.get_int(key, expected, expected, expected));
    };
    const auto port = [&](const std::string& key, int fallback) {
        return static_cast<uint16_t>(config_.get_int(key, fallback, 1, std::numeric_limits<uint16_t>::max()));
    };
    const auto nonempty = [&](const std::string& key, const std::string& fallback) {
        const auto value = config_.get_string(key, fallback);
        if (value.empty()) config_.reject_value(key, "a nonempty string");
        return value;
    };
    const auto address = [&](const std::string& key) {
        const auto value = config_.get_string(key, "0.0.0.0");
        in_addr parsed{};
        if (inet_pton(AF_INET, value.c_str(), &parsed) != 1) config_.reject_value(key, "an IPv4 address");
        return value;
    };
    const uint32_t input_rate = fixed_integer("input.contract.sample_rate", MOZART_INPUT_SAMPLE_RATE);
    const uint32_t output_rate = fixed_integer("output.sample_rate", MOZART_OUTPUT_SAMPLE_RATE);
    const uint32_t frame_ms = fixed_integer("network.audio.frame_duration_ms", MOZART_INPUT_FRAME_MS);
    const bool rvc_enabled = config_.get_bool("rvc.enabled", true);
    const rvc::RvcMockConfig mock{
        config_.get_bool("rvc.mock.generator", false),
        config_.get_bool("rvc.mock.hubert", false),
        config_.get_bool("rvc.mock.rmvpe", false)
    };
    const auto models_dir = config_.resolve_file_path("rvc.models_dir", "./models");
    const auto hubert_path = config_.resolve_file_path("rvc.hubert_path", "./assets/hubert/hubert_base.onnx");
    const auto optional_path = [&](const std::string& key) -> std::optional<std::filesystem::path> {
        const auto path = config_.resolve_file_path(key, "", true);
        return path.empty() ? std::nullopt : std::optional<std::filesystem::path>(path);
    };
    const auto rmvpe_path = optional_path("rvc.rmvpe_path");
    const auto realtime_hubert_path = optional_path("rvc.realtime_hubert_path");
    const auto realtime_rmvpe_path = optional_path("rvc.realtime_rmvpe_path");
    const auto device = config_.get_string("rvc.device", "cuda");
    if (device != "cpu" && device != "cuda") config_.reject_value("rvc.device", "cpu or cuda");
    const bool half = config_.get_bool("rvc.half", false);
    rvc::RvcParameters default_parameters;
    default_parameters.f0_method = config_.get_string("rvc.f0_method", default_parameters.f0_method);
    default_parameters.pitch_shift = config_.get_int("rvc.pitch_shift", default_parameters.pitch_shift);
    default_parameters.index_rate = config_.get_double("rvc.index_rate", default_parameters.index_rate);
    default_parameters.index_nprobe = config_.get_int("rvc.index_nprobe", default_parameters.index_nprobe);
    default_parameters.filter_radius = config_.get_int("rvc.filter_radius", default_parameters.filter_radius);
    default_parameters.rms_mix_rate = config_.get_double("rvc.rms_mix_rate", default_parameters.rms_mix_rate);
    default_parameters.protect = config_.get_double("rvc.protect", default_parameters.protect);
    if (const char* invalid = rvc::invalid_rvc_parameter(default_parameters)) {
        config_.reject_value(std::string("rvc.") + invalid, "a supported RVC parameter value");
    }

    rvc::ModeController::Config controller_config;
    controller_config.rvc_enabled = rvc_enabled;
    controller_config.audio_host = address("network.audio.host");
    controller_config.audio_port = port("network.audio.port", 18000);
    controller_config.input_sample_rate = input_rate;
    controller_config.output_sample_rate = output_rate;
    controller_config.frame_duration_ms = frame_ms;
    controller_config.skip_silence = config_.get_bool("input.meta.vad_enabled", true);
    const int playout_delay = config_.get_int("rvc.realtime_playout_delay_ms", 400, 0, 1000);
    if (playout_delay % MOZART_INPUT_FRAME_MS != 0) {
        config_.reject_value("rvc.realtime_playout_delay_ms", "a multiple of " + std::to_string(MOZART_INPUT_FRAME_MS) + " ms");
    }
    controller_config.realtime_playout_delay_ms = static_cast<uint32_t>(playout_delay);
    controller_config.file_denoise = config_.get_bool("storage.file_denoise", false);
    controller_config.storage_dir = config_.resolve_file_path("storage.temp_dir", "./storage/temp");
    controller_config.ffmpeg_path = nonempty("storage.ffmpeg_path", "ffmpeg");
    controller_config.max_queue_depth = static_cast<size_t>(config_.get_int("storage.max_queue_depth", 50, 1, std::numeric_limits<int>::max()));
    controller_config.max_cache_bytes = static_cast<uint64_t>(config_.get_int("storage.max_cache_size_mb", 1000, 0, std::numeric_limits<int>::max())) * 1024ULL * 1024ULL;
    controller_config.models_dir = models_dir;
    controller_config.presets_path = config_.resolve_file_path("storage.presets_path", "./storage/presets.json");
    const auto control_host = address("network.control.host");
    const auto control_port = port("network.control.port", 18080);
    controller_config.default_parameters = default_parameters;

    spdlog::info("RVC config: generator={}, hubert={}, rmvpe={}, models={}",
                 !rvc_enabled || mock.generator ? "mock" : "real",
                 !rvc_enabled || mock.hubert ? "mock" : "real",
                 !rvc_enabled || mock.rmvpe ? "mock" : "real", models_dir.string());
    if (rvc_enabled) {
        pipeline_ = rvc::RVCPipelineFactory::create(
            mock, models_dir, hubert_path, rmvpe_path,
            input_rate, output_rate, device, half, default_parameters,
            realtime_hubert_path, realtime_rmvpe_path);
    } else {
        pipeline_ = std::make_unique<rvc::MockRVCPipeline>(input_rate, output_rate);
    }
    controller_ = std::make_unique<rvc::ModeController>(*pipeline_, std::move(controller_config));

    api_ = std::make_unique<rvc::HttpApiServer>(
        control_host, control_port,
        controller_.get());
    if (!api_->start()) {
        api_.reset();
        controller_.reset();
        pipeline_.reset();
        return false;
    }
    // M4 并存期：同一表面的二进制协议服务（迁移第一步，HTTP 保留）。
    const std::string control_socket = config_.get_string("network.control.socket", "");
    if (!control_socket.empty()) {
        control_api_ = std::make_unique<rvc::ControlApiServer>(control_socket,
                                                               controller_.get());
        if (!control_api_->start()) control_api_.reset();
    }
    started_ = true;
    spdlog::info("state daemon started; initial mode is IDLE");
    return true;
}

void StateManagerDaemon::stop() {
    if (!started_) return;
    if (control_api_) {
        control_api_->stop();
        control_api_.reset();
    }
    api_->stop();
    api_.reset();
    controller_.reset();
    pipeline_.reset();
    started_ = false;
}

} // namespace mozart
