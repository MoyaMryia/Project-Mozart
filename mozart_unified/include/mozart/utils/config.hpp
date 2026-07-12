#pragma once
#include <string>
#include <mozart/rvc/rvc_pipeline.hpp>
#include <mozart/source/udp_source.hpp>
#include <mozart/output/pipewire_sink.hpp>
#include <mozart/output/udp_sink.hpp>
#include <mozart/server/http_server.hpp>

namespace mozart {

// Unified configuration loaded from TOML
struct Config {
    // Audio settings
    struct Audio {
        uint32_t input_rate = 16000;
        uint32_t output_rate = 48000;
        size_t frame_ms = 20;
    } audio;

    // Source selection
    struct Source {
        std::string type = "mock";  // "mock", "ring", "udp"
        std::string ring_name;
        UdpSource::Config udp;
    } source;

    // RVC inference
    struct RVC {
        bool mock_mode = true;
        std::string models_dir = "models";
        std::string hubert_model;
        RVCParams params;
    } rvc;

    // Output sink
    struct Output {
        std::string type = "dummy";  // "dummy", "pipewire", "udp"
        PipeWireSink::Config pipewire;
        UdpSink::Config udp;
    } output;

    // HTTP server
    struct Server {
        bool enabled = true;
        HttpServer::Config config;
    } server;

    // Load from TOML file
    static Config load(const std::string& path);
};

}  // namespace mozart
