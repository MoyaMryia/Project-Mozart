// utils/config.cpp
#include "utils/config.hpp"
#include "utils/logging.hpp"

#include <toml++/toml.h>

#include <stdexcept>

namespace mozart {

namespace {

template <typename T>
T get_or(const toml::node& node, std::string_view key, T def) {
    if (auto v = node[key].value<T>()) return static_cast<T>(*v);
    return def;
}

} // namespace

AppConfig load_config(const std::filesystem::path& path) {
    AppConfig cfg;

    toml::table root;
    try {
        root = toml::parse_file(path.string());
    } catch (const std::exception& e) {
        throw std::runtime_error(std::string("Failed to parse ") +
                                 path.string() + ": " + e.what());
    }

    if (auto a = root["audio"].as_table()) {
        cfg.audio.input_rate  = get_or<int>(*a, "input_rate",  16000);
        cfg.audio.output_rate = get_or<int>(*a, "output_rate", 48000);
        cfg.audio.channels    = get_or<int>(*a, "channels",    1);
        cfg.audio.frame_ms    = get_or<int>(*a, "frame_ms",    20);
    }

    if (auto c = root["contract"].as_table()) {
        cfg.contract.source          = get_or<std::string>(*c, "source", "mock");
        cfg.contract.ring_name       = get_or<std::string>(*c, "ring_name", "mozart_pre_out");
        cfg.contract.poll_timeout_us = get_or<int>(*c, "poll_timeout_us", 5000);
    }

    if (auto r = root["rvc"].as_table()) {
        cfg.rvc.assets_dir  = get_or<std::string>(*r, "assets_dir",  "./assets");
        cfg.rvc.models_dir  = get_or<std::string>(*r, "models_dir",  "./models");
        cfg.rvc.hubert_path = get_or<std::string>(*r, "hubert_path", "./assets/hubert_base.onnx");
        cfg.rvc.rmvpe_path  = get_or<std::string>(*r, "rmvpe_path",  "./assets/rmvpe.onnx");
        cfg.rvc.device      = get_or<std::string>(*r, "device",      "cuda");
        cfg.rvc.index_rate  = get_or<float>(*r, "index_rate",        0.0f);
        cfg.rvc.mock_mode   = get_or<bool>(*r, "mock_mode",          true);
    }

    if (auto o = root["output"].as_table()) {
        cfg.output.sink        = get_or<std::string>(*o, "sink",        "dummy");
        cfg.output.source_name = get_or<std::string>(*o, "source_name", "rvc_voice_out");
    }

    if (auto s = root["server"].as_table()) {
        cfg.server.host = get_or<std::string>(*s, "host", "0.0.0.0");
        cfg.server.port = get_or<int>(*s, "port", 18080);
    }

    MOZART_INFO("config loaded: input=%d out=%d frame=%dms source=%s sink=%s mode=%s",
                cfg.audio.input_rate, cfg.audio.output_rate, cfg.audio.frame_ms,
                cfg.contract.source.c_str(), cfg.output.sink.c_str(),
                cfg.rvc.mock_mode ? "mock" : "real");
    return cfg;
}

} // namespace mozart