#include "rvc/pipeline.hpp"
#include "rvc/onnx_engine.hpp"
#include <spdlog/spdlog.h>
#include <nlohmann/json.hpp>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>

int main() {
    spdlog::set_level(spdlog::level::off);
    char temporary[] = "/tmp/mozart-asset-probe-XXXXXX";
    const auto created = mkdtemp(temporary);
    if (!created) return 1;
    const std::filesystem::path root(created);
    std::filesystem::create_directory(root / "a");
    std::filesystem::create_directory(root / "b");
    auto entry = std::filesystem::directory_iterator(root);
    const auto first = entry->path();
    ++entry;
    const auto second = entry->path();
    std::ofstream(first / "config.json") << "{}";
    std::ofstream(first / (first.filename().string() + ".pth")) << "probe";
    std::ofstream(second / "config.json") << "{}";
    const auto onnx = second / (second.filename().string() + ".onnx");
    std::ofstream(onnx) << "deliberately not an ONNX model";
    auto pipeline = rvc::RVCPipelineFactory::create(
        {false, true, true}, root, root / "missing-hubert.onnx",
        std::nullopt, 16000, 48000, "cpu", false, {});
    const bool factory_empty = pipeline->current_model_id().empty();
    const bool explicit_load = pipeline->switch_model(second.filename().string());
    rvc::OnnxEngine engine;
    const bool stub_loaded = engine.load(onnx);
    bool execution_failed = false;
    try {
        engine.run(std::vector<rvc::OnnxInput>{}, {"audio"});
    } catch (const std::exception&) {
        execution_failed = true;
    }
    std::cout << nlohmann::json{
        {"factory_skips_later_loadable_model", factory_empty && explicit_load},
        {"stub_accepts_invalid_onnx_file", stub_loaded},
        {"stub_execution_throws", execution_failed}
    }.dump(2) << '\n';
    std::filesystem::remove_all(root);
}
