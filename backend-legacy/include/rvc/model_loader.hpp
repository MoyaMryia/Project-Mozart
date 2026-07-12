#pragma once

#include <string>
#include <filesystem>
#include <optional>
#include <vector>
#include <memory>
#include <map>

namespace rvc {

// ──────────────────────────────────────────────────────────
// RVC Model Configuration
// ──────────────────────────────────────────────────────────
struct RVCModelConfig {
    uint32_t sample_rate = 48000;
    uint32_t emb_channels = 768;
    uint32_t spk_id = 0;
    bool has_f0 = true;

    static std::optional<RVCModelConfig> from_json(const std::filesystem::path& path);
};

// ──────────────────────────────────────────────────────────
// Single loaded RVC model
// ──────────────────────────────────────────────────────────
class RVCModel {
public:
    RVCModel(const std::string& model_id, const std::filesystem::path& model_dir);

    bool exists() const;
    bool load(const std::string& device = "cuda", bool half = false);

    const std::string& id() const { return model_id_; }
    bool loaded() const { return loaded_; }
    const RVCModelConfig& config() const { return config_; }

    const std::filesystem::path& pth_path() const { return pth_path_; }
    const std::filesystem::path& index_path() const { return index_path_; }
    const std::filesystem::path& config_path() const { return config_path_; }

private:
    std::string model_id_;
    std::filesystem::path model_dir_;
    std::filesystem::path pth_path_;
    std::filesystem::path index_path_;
    std::filesystem::path config_path_;

    RVCModelConfig config_;
    bool loaded_ = false;

    bool load_generator(const std::string& device, bool half);
    bool load_index();
};

// ──────────────────────────────────────────────────────────
// Model manager: handles multiple models and hot-switching
// ──────────────────────────────────────────────────────────
class ModelManager {
public:
    ModelManager(
        const std::filesystem::path& models_dir,
        const std::string& device = "cuda",
        bool half = false
    );

    std::vector<std::map<std::string, std::string>> list_models() const;
    std::shared_ptr<RVCModel> get_model(const std::string& model_id);
    bool load_model(const std::string& model_id);
    std::shared_ptr<RVCModel> current_model() const;
    std::string current_model_id() const { return current_model_id_; }

private:
    std::filesystem::path models_dir_;
    std::string device_;
    bool half_;
    std::map<std::string, std::shared_ptr<RVCModel>> models_;
    std::string current_model_id_;
};

} // namespace rvc
