// rvc/model_loader.hpp
// Loads speaker ONNX model details (generator path + config.json).
// .index retrieval is OUT OF SCOPE this phase (index_rate forced 0).
#pragma once

#include <filesystem>
#include <string>
#include <vector>

namespace mozart {

struct RVCModelInfo {
    std::string         id;
    std::filesystem::path generator_path;   // {id}.onnx
    int                 sample_rate = 48000;
    int                 emb_channels = 768;
    int                 spk_id = 0;
    bool                f0 = true;
    bool                loaded = false;
};

class ModelManager {
public:
    explicit ModelManager(std::filesystem::path models_dir);

    // Scan disk and return a list of model ids (directories with {id}.onnx).
    std::vector<RVCModelInfo> list_models() const;

    // Parse config.json + resolve generator path. Does NOT load weights here.
    bool get_model(const std::string& id, RVCModelInfo& out) const;

    // Mark a model id as the active one. Returns false if absent.
    bool activate(const std::string& id);

    const std::string& current_model_id() const { return current_; }
    void               clear_current() { current_.clear(); }

private:
    std::filesystem::path models_dir_;
    std::string            current_;
};

} // namespace mozart