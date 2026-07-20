#pragma once

#include <cstdint>
#include <string>
#include <vector>
#include <memory>
#include <map>
#include <filesystem>
#include <optional>

#include "rvc/onnx_engine.hpp"
#include "rvc/index_search.hpp"

namespace rvc {

struct RVCModelConfig {
    uint32_t sample_rate = 48000;
    uint32_t emb_channels = 768;
    uint32_t spk_id = 0;
    bool has_f0 = true;

    static std::optional<RVCModelConfig> from_json(const std::filesystem::path& path);
};

class RVCModel {
public:
    RVCModel(const std::string& model_id, const std::filesystem::path& model_dir);

    const std::string& id() const { return model_id_; }
    const RVCModelConfig& config() const { return config_; }
    bool exists() const;
    bool loaded() const { return loaded_; }

    bool load(const std::string& device = "cuda", bool half = false);
    void unload();

    OnnxEngine& generator_engine() { return generator_engine_; }
    IndexSearch& index() { return index_; }

private:
    std::string model_id_;
    std::filesystem::path model_dir_;
    std::filesystem::path pth_path_;
    std::filesystem::path onnx_path_;
    std::filesystem::path index_path_;
    std::filesystem::path config_path_;
    RVCModelConfig config_;
    bool loaded_ = false;

    OnnxEngine generator_engine_;
    IndexSearch index_;

    bool load_generator(const std::string& device, bool half);
    bool load_index();
};

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

private:
    std::filesystem::path models_dir_;
    std::string device_;
    bool half_;
    std::string current_model_id_;
    std::map<std::string, std::shared_ptr<RVCModel>> models_;
};

} // namespace rvc
