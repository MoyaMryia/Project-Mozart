// utils/config.hpp
// Loads config.toml and exposes typed accessors. Built on toml++ (header-only).
#pragma once

#include <string>
#include <filesystem>

namespace mozart {

struct AudioConfig {
    int input_rate  = 16000;
    int output_rate = 48000;
    int channels    = 1;
    int frame_ms    = 20;
};

struct ContractConfig {
    std::string source         = "mock";
    std::string ring_name      = "mozart_pre_out";
    int         poll_timeout_us = 5000;
};

struct RvcConfig {
    std::string assets_dir  = "./assets";
    std::string models_dir  = "./models";
    std::string hubert_path = "./assets/hubert_base.onnx";
    std::string rmvpe_path  = "./assets/rmvpe.onnx";
    std::string device      = "cuda";
    float       index_rate  = 0.0f;
    bool        mock_mode   = true;
};

struct OutputConfig {
    std::string sink        = "dummy";
    std::string source_name = "rvc_voice_out";
};

struct ServerConfig {
    std::string host = "0.0.0.0";
    int         port = 18080;
};

struct AppConfig {
    AudioConfig    audio;
    ContractConfig contract;
    RvcConfig      rvc;
    OutputConfig   output;
    ServerConfig   server;
};

// Parse a TOML file. Throws std::runtime_error on missing keys / IO errors.
AppConfig load_config(const std::filesystem::path& path);

} // namespace mozart