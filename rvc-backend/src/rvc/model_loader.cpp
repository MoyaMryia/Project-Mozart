#include "rvc/model_loader.hpp"
#include <spdlog/spdlog.h>
#include <fstream>
#include <nlohmann/json.hpp>

namespace rvc {

std::optional<RVCModelConfig> RVCModelConfig::from_json(const std::filesystem::path& path) {
    std::ifstream f(path);
    if (!f) {
        spdlog::error("Cannot open config: {}", path.string());
        return std::nullopt;
    }

    try {
        nlohmann::json j;
        f >> j;

        RVCModelConfig cfg;
        if (j.contains("data") && j["data"].contains("sampling_rate")) {
            cfg.sample_rate = j["data"]["sampling_rate"].get<uint32_t>();
        }
        if (j.contains("model") && j["model"].contains("emb_channels")) {
            cfg.emb_channels = j["model"]["emb_channels"].get<uint32_t>();
        }
        if (j.contains("spk") && j["spk"].contains("id")) {
            cfg.spk_id = j["spk"]["id"].get<uint32_t>();
        }
        if (j.contains("f0")) {
            cfg.has_f0 = j["f0"].get<bool>();
        }
        return cfg;
    } catch (const std::exception& e) {
        spdlog::error("Failed to parse config {}: {}", path.string(), e.what());
        return std::nullopt;
    }
}

RVCModel::RVCModel(const std::string& model_id, const std::filesystem::path& model_dir)
    : model_id_(model_id)
    , model_dir_(model_dir)
    , pth_path_(model_dir / (model_id + ".pth"))
    , onnx_path_(model_dir / (model_id + ".onnx"))
    , index_path_(model_dir / (model_id + ".index"))
    , config_path_(model_dir / "config.json")
{}

bool RVCModel::exists() const {
    return std::filesystem::exists(config_path_) &&
           (std::filesystem::exists(onnx_path_) || std::filesystem::exists(pth_path_));
}

bool RVCModel::load(const std::string& device, bool half) {
    if (!exists()) {
        spdlog::error("Model files missing for {}", model_id_);
        return false;
    }

    try {
        auto cfg = RVCModelConfig::from_json(config_path_);
        if (!cfg) return false;
        config_ = *cfg;

        if (!load_generator(device, half)) {
            return false;
        }

        if (std::filesystem::exists(index_path_)) {
            load_index();
        }

        loaded_ = true;
        spdlog::info("RVC model '{}' loaded (sr={}, emb={}) on {}",
            model_id_, config_.sample_rate, config_.emb_channels, device);
        return true;

    } catch (const std::exception& e) {
        spdlog::error("Failed to load RVC model {}: {}", model_id_, e.what());
        loaded_ = false;
        return false;
    }
}

void RVCModel::unload() {
    generator_engine_.unload();
    loaded_ = false;
}

bool RVCModel::load_generator(const std::string& device, bool half) {
    if (std::filesystem::exists(onnx_path_)) {
        spdlog::info("Loading ONNX generator: {}", onnx_path_.string());
        return generator_engine_.load(onnx_path_);
    }

    spdlog::warn("ONNX model not found at {}; trying .pth fallback (needs libtorch)",
                 onnx_path_.string());
#ifdef USE_LIBTORCH
    throw std::runtime_error("libtorch .pth loading not yet implemented");
#else
    spdlog::error("No ONNX model and USE_LIBTORCH=OFF; cannot load generator");
    return false;
#endif
}

bool RVCModel::load_index() {
    spdlog::info("Loading index: {}", index_path_.string());
    return index_.load(index_path_);
}

ModelManager::ModelManager(
    const std::filesystem::path& models_dir,
    const std::string& device,
    bool half
) : models_dir_(models_dir), device_(device), half_(half)
{
    std::filesystem::create_directories(models_dir_);
}

std::vector<std::map<std::string, std::string>> ModelManager::list_models() const {
    std::vector<std::map<std::string, std::string>> result;
    if (!std::filesystem::exists(models_dir_)) return result;

    for (const auto& entry : std::filesystem::directory_iterator(models_dir_)) {
        if (!entry.is_directory()) continue;

        auto model = std::make_shared<RVCModel>(
            entry.path().filename().string(), entry.path()
        );
        result.push_back({
            {"id", model->id()},
            {"exists", model->exists() ? "true" : "false"},
            {"loaded", model->loaded() ? "true" : "false"},
            {"current", (model->id() == current_model_id_) ? "true" : "false"},
        });
    }
    return result;
}

std::shared_ptr<RVCModel> ModelManager::get_model(const std::string& model_id) {
    auto it = models_.find(model_id);
    if (it != models_.end()) return it->second;

    auto model_dir = models_dir_ / model_id;
    if (!std::filesystem::exists(model_dir)) return nullptr;

    auto model = std::make_shared<RVCModel>(model_id, model_dir);
    models_[model_id] = model;
    return model;
}

bool ModelManager::load_model(const std::string& model_id) {
    auto model = get_model(model_id);
    if (!model) {
        spdlog::error("Model {} not found", model_id);
        return false;
    }
    bool success = model->load(device_, half_);
    if (success) {
        current_model_id_ = model_id;
    }
    return success;
}

std::shared_ptr<RVCModel> ModelManager::current_model() const {
    if (current_model_id_.empty()) return nullptr;
    auto it = models_.find(current_model_id_);
    if (it != models_.end()) return it->second;
    return nullptr;
}

} // namespace rvc
