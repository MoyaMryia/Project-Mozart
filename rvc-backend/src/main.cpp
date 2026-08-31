#include <csignal>
#include <thread>
#include <chrono>
#include <iostream>
#include <spdlog/spdlog.h>
#include <spdlog/sinks/stdout_color_sinks.h>

#include "utils/config.hpp"
#include "mozart/http_api.hpp"
#include "rvc/pipeline.hpp"
#include "state/mode_controller.hpp"
#include "state/log_store.hpp"

using namespace rvc;

std::atomic<bool> g_shutdown{false};

void signal_handler(int signum) {
    spdlog::info("Received signal {}, shutting down...", signum);
    g_shutdown.store(true);
}

int main(int argc, char* argv[]) {
    // Setup logging
    auto console = spdlog::stdout_color_mt("console");
    console->set_level(spdlog::level::info);
    console->set_pattern("%Y-%m-%d %H:%M:%S.%e [%^%l%$] %v");
    spdlog::set_default_logger(console);
    install_backend_log_sink();

    spdlog::info("=== RVC Voice Changer Backend (C++) ===");
    spdlog::info("Role: Receives preprocessed contract-stream audio, runs RVC inference");

    // Load configuration
    Config config;
    try {
        config = argc > 1 ? Config::from_yaml(argv[1]) : Config::default_config();
    } catch (const std::exception& e) {
        spdlog::error("Failed to load config: {}", e.what());
        return 1;
    }

    // Input contract settings (from Project Mozart)
    uint32_t input_sample_rate = config.get_int("input.contract.sample_rate", 16000);
    uint32_t frame_duration_ms = config.get_int("network.audio.frame_duration_ms", 20);
    bool skip_silence = config.get_bool("input.meta.vad_enabled", true);
    // Output settings (RVC generator output)
    uint32_t output_sample_rate = config.get_int("output.sample_rate", 48000);

    // RVC configuration
    std::string models_dir = config.get_string("rvc.models_dir", "./models");
    std::string hubert_path = config.get_string("rvc.hubert_path", "./assets/hubert/hubert_base.pt");
    std::string rmvpe_path_str = config.get_string("rvc.rmvpe_path", "");
    std::optional<std::filesystem::path> rmvpe_path;
    if (!rmvpe_path_str.empty()) {
        rmvpe_path = rmvpe_path_str;
    }
    RvcMockConfig mock{
        config.get_bool("rvc.mock.generator", false),
        config.get_bool("rvc.mock.hubert", false),
        config.get_bool("rvc.mock.rmvpe", false)
    };
    std::string device = config.get_string("rvc.device", "cuda");
    bool half = config.get_bool("rvc.half", false);

    // Network configuration
    std::string audio_host = config.get_string("network.audio.host", "0.0.0.0");
    uint16_t audio_port = static_cast<uint16_t>(config.get_int("network.audio.port", 18000));
    std::string api_host = config.get_string("network.control.host", "0.0.0.0");
    uint16_t api_port = static_cast<uint16_t>(config.get_int("network.control.port", 18080));
    bool print_latency = config.get_bool("logging.print_latency_stats", true);
    int latency_interval_sec = config.get_int("logging.latency_stats_interval_sec", 5);

    // Create RVC pipeline
    auto pipeline = RVCPipelineFactory::create(
        mock,
        models_dir,
        hubert_path,
        rmvpe_path,
        input_sample_rate,
        output_sample_rate,
        device,
        half
    );

    ModeController::Config controller_config;
    controller_config.audio_host = audio_host;
    controller_config.audio_port = audio_port;
    controller_config.input_sample_rate = input_sample_rate;
    controller_config.output_sample_rate = output_sample_rate;
    controller_config.frame_duration_ms = frame_duration_ms;
    controller_config.skip_silence = skip_silence;
    controller_config.file_rnnoise = config.get_bool("storage.file_rnnoise", false);
    controller_config.storage_dir = config.get_string("storage.temp_dir", "./storage/temp");
    controller_config.ffmpeg_path = config.get_string("storage.ffmpeg_path", "ffmpeg");
    controller_config.max_queue_depth = static_cast<size_t>(config.get_int("storage.max_queue_depth", 50));
    controller_config.max_cache_bytes = static_cast<uint64_t>(config.get_int("storage.max_cache_size_mb", 1000)) * 1024ULL * 1024ULL;
    ModeController controller(*pipeline, controller_config);

    // Setup HTTP API server
    HttpApiServer api_server(
        api_host, api_port,
        &controller
    );

    // Signal handling
    std::signal(SIGINT, signal_handler);
    std::signal(SIGTERM, signal_handler);

    // Start servers
    try {
        if (!api_server.start()) {
            throw std::runtime_error("failed to start HTTP API server");
        }
    } catch (const std::exception& e) {
        spdlog::error("Server startup failed: {}", e.what());
        return 1;
    }

    spdlog::info(
        "RVC Voice Changer Backend started. "
        "Contract input: {}Hz, RVC output: {}Hz, frame={}ms, mode={}",
        input_sample_rate, output_sample_rate, frame_duration_ms,
        mock.generator || mock.hubert || mock.rmvpe ? "partial-mock" : "real"
    );
    spdlog::info("UDP audio: {}:{}", audio_host, audio_port);
    spdlog::info("HTTP API: {}:{}", api_host, api_port);

    // Main loop: print latency stats periodically
    while (!g_shutdown.load()) {
        if (print_latency) {
            const auto status = controller.status();
            spdlog::info("Mode: {}, queued jobs: {}", status.value("mode", "idle"), status["queue"].size());
        }

        for (int i = 0; i < latency_interval_sec * 10 && !g_shutdown.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    }

    // Shutdown
    api_server.stop();
    spdlog::info("Shutdown complete");
    return 0;
}
