#include <mozart/rvc/model_loader.hpp>
#include <mozart/utils/logging.hpp>
#include <nlohmann/json.hpp>
#include <filesystem>
#include <fstream>

namespace fs = std::filesystem;

namespace mozart {

ModelManager::ModelManager(const std::string& models_dir)
    : models_dir_(models_dir) {}

void ModelManager::scan() {
    models_.clear();

    if (!fs::exists(models_dir_)) {
        MOZART_LOG_WARN("Models directory does not exist: {}", models_dir_);
        return;
    }

    for (const auto& entry : fs::directory_iterator(models_dir_)) {
        if (!entry.is_directory()) continue;

        RVCModel model;
        model.id = entry.path().filename().string();

        // Look for model files
        for (const auto& file : fs::directory_iterator(entry.path())) {
            std::string ext = file.path().extension().string();
            if (ext == ".pth") {
                model.model_path = file.path().string();
            } else if (ext == ".index") {
                model.index_path = file.path().string();
            } else if (file.path().filename() == "config.json") {
                model.config_path = file.path().string();
            }
        }

        if (model.model_path.empty()) {
            MOZART_LOG_WARN("Model {} has no .pth file, skipping", model.id);
            continue;
        }

        // Load config.json if present
        if (!model.config_path.empty()) {
            std::ifstream f(model.config_path);
            if (f.is_open()) {
                nlohmann::json j;
                f >> j;
                model.config.name = j.value("name", model.id);
                model.config.speaker_id = j.value("speaker_id", "");
                model.config.sample_rate = j.value("sample_rate", 48000);
                model.config.pitch_guidance = j.value("pitch_guidance", 1);
                model.config.version = j.value("version", "v2");
            }
        }

        MOZART_LOG_INFO("Discovered model: {} ({})", model.id, model.config.name);
        models_.push_back(std::move(model));
    }

    MOZART_LOG_INFO("Found {} models in {}", models_.size(), models_dir_);
}

std::vector<std::string> ModelManager::list_models() const {
    std::vector<std::string> ids;
    for (const auto& m : models_) {
        ids.push_back(m.id);
    }
    return ids;
}

std::optional<RVCModel> ModelManager::get_model(const std::string& id) {
    for (const auto& m : models_) {
        if (m.id == id) return m;
    }
    return std::nullopt;
}

bool ModelManager::set_active(const std::string& id) {
    for (const auto& m : models_) {
        if (m.id == id) {
            active_id_ = id;
            MOZART_LOG_INFO("Activated model: {}", id);
            return true;
        }
    }
    return false;
}

std::optional<RVCModel> ModelManager::active_model() const {
    if (active_id_.empty()) return std::nullopt;
    return get_model(active_id_);
}

}  // namespace mozart
