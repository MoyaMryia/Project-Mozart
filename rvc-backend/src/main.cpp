#include <csignal>
#include <thread>
#include <chrono>
#include <iostream>
#include <spdlog/spdlog.h>
#include <spdlog/sinks/stdout_color_sinks.h>

#include "utils/config.hpp"
#include "network/udp_server.hpp"
#include "api/http_api.hpp"
#include "rvc/pipeline.hpp"

using namespace rvc;

std::atomic<bool> g_shutdown{false};

void signal_handler(int signum) {
    spdlog::info("Received signal {}, shutting down...", signum);
    g_shutdown.store(true);
}

int main() {
    // Setup logging
    auto console = spdlog::stdout_color_mt("console");
    console->set_level(spdlog::level::info);
    console->set_pattern("%Y-%m-%d %H:%M:%S.%e [%^%l%$] %v");
    spdlog::set_default_logger(console);

    spdlog::info("=== RVC Voice Changer Backend (C++) ===");
    spdlog::info("Role: Receives preprocessed contract-stream audio, runs RVC inference");

    // Load configuration
    Config config;
    try {
        config = Config::default_config();
    } catch (const std::exception& e) {
        spdlog::error("Failed to load config: {}", e.what());
        return 1;
    }

    // Input contract settings (from Project Mozart)
    uint32_t input_sample_rate = config.get_int("input.contract.sample_rate", 16000);
    uint32_t frame_duration_ms = config.get_int("network.audio.frame_duration_ms", 20);
    bool skip_silence = config.get_bool("input.meta.vad_enabled", true);
    uint32_t frames_per_inference = config.get_int("network.audio.frames_per_inference", 1);

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
    bool mock_mode = config.get_bool("rvc.mock_mode", true);
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
        mock_mode,
        models_dir,
        hubert_path,
        rmvpe_path,
        input_sample_rate,
        output_sample_rate,
        device,
        half
    );

    // Inference callback: pipeline receives float32, returns float32
    auto inference_callback = [&pipeline](const std::vector<float>& audio) -> std::vector<float> {
        return pipeline->process(audio);
    };

    // Setup UDP contract-stream server
    UdpAudioServer udp_server(
        audio_host, audio_port,
        input_sample_rate,
        output_sample_rate,
        frame_duration_ms,
        inference_callback,
        skip_silence,
        frames_per_inference
    );

    // Setup HTTP API server
    HttpApiServer api_server(
        api_host, api_port,
        pipeline.get(),
        &udp_server,
        models_dir
    );

    // Signal handling
    std::signal(SIGINT, signal_handler);
    std::signal(SIGTERM, signal_handler);

    // Start servers
    try {
        udp_server.start();
        api_server.start();
    } catch (const std::exception& e) {
        spdlog::error("Server startup failed: {}", e.what());
        return 1;
    }

    spdlog::info(
        "RVC Voice Changer Backend started. "
        "Contract input: {}Hz, RVC output: {}Hz, frame={}ms, mode={}",
        input_sample_rate, output_sample_rate, frame_duration_ms,
        mock_mode ? "mock" : "real"
    );
    spdlog::info("UDP audio: {}:{}", audio_host, audio_port);
    spdlog::info("HTTP API: {}:{}", api_host, api_port);

    // Main loop: print latency stats periodically
    while (!g_shutdown.load()) {
        if (print_latency) {
            auto stats = udp_server.get_latency_stats();
            auto bypass = udp_server.get_bypass_stats();
            if (stats.count > 0) {
                spdlog::info(
                    "Latency: count={}, avg={:.2f}ms, max={:.2f}ms | "
                    "Bypass: {} silent frames",
                    stats.count, stats.avg_ms, stats.max_ms,
                    bypass.bypass_count
                );
            }
        }

        for (int i = 0; i < latency_interval_sec * 10 && !g_shutdown.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    }

    // Shutdown
    udp_server.stop();
    api_server.stop();
    spdlog::info("Shutdown complete");
    return 0;
}
