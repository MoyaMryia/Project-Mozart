#pragma once
#include <string>
#include <vector>
#include <optional>

namespace mozart {

// RVC model configuration (from config.json)
struct RVCModelConfig {
    std::string name;
    std::string speaker_id;
    int sample_rate = 48000;
    int pitch_guidance = 1;
    std::string version;  // "v1" or "v2"
};

// RVC model files
struct RVCModel {
    std::string id;              // Model directory name
    std::string model_path;      // .pth file path
    std::string index_path;      // .index file path (optional)
    std::string config_path;     // config.json path
    RVCModelConfig config;

    bool has_index() const { return !index_path.empty(); }
};

// Model manager: scans directory, loads models on demand
class ModelManager {
public:
    explicit ModelManager(const std::string& models_dir);

    // Scan models directory and discover available models
    void scan();

    // Get list of available model IDs
    std::vector<std::string> list_models() const;

    // Get model by ID (loads config.json if not already loaded)
    std::optional<RVCModel> get_model(const std::string& id);

    // Set active model
    bool set_active(const std::string& id);

    // Get currently active model
    std::optional<RVCModel> active_model() const;

private:
    std::string models_dir_;
    std::vector<RVCModel> models_;
    std::string active_id_;
};

}  // namespace mozart
