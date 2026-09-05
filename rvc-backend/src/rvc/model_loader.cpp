#include "rvc/model_loader.hpp"
#include <spdlog/spdlog.h>
#include <fstream>
#include <nlohmann/json.hpp>

namespace rvc {

namespace {

bool warmup_fixed_engine(
    IEngine& engine,
    const std::vector<const char*>& names,
    const char* output
) {
    std::vector<OnnxInput> inputs;
    inputs.reserve(names.size());
    int64_t sequence_frames = 0;
    const auto feats_shape = engine.input_shape("feats");
    if (feats_shape.size() >= 2) sequence_frames = feats_shape[1];
    for (const char* name : names) {
        auto shape = engine.input_shape(name);
        size_t elements = 1;
        for (const int64_t dimension : shape) {
            if (dimension <= 0) return false;
            elements *= static_cast<size_t>(dimension);
        }
        const auto type = engine.input_type(name).value_or(OnnxInput::Type::Float);
        std::vector<float> floats;
        std::vector<int64_t> integers;
        if (type == OnnxInput::Type::Int64) {
            integers.assign(elements, 0);
            if (std::string(name) == "p_len") {
                std::fill(integers.begin(), integers.end(), sequence_frames);
            } else if (std::string(name) == "pitch") {
                std::fill(integers.begin(), integers.end(), 1);
            }
        } else {
            floats.assign(elements, 0.0f);
            if (std::string(name) == "p_len") {
                std::fill(floats.begin(), floats.end(), static_cast<float>(sequence_frames));
            } else if (std::string(name) == "pitch") {
                std::fill(floats.begin(), floats.end(), 1.0f);
            }
        }
        inputs.push_back({
            name, std::move(shape), type, std::move(floats), std::move(integers)
        });
    }
    try {
        engine.run(inputs, {output});
        return true;
    } catch (const std::exception& error) {
        spdlog::error("Realtime engine warmup failed: {}", error.what());
        return false;
    }
}

} // namespace

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
    , realtime_front_path_(model_dir / (model_id + "-front.onnx"))
    , realtime_decoder_path_(model_dir / (model_id + "-decoder.onnx"))
    , index_path_(model_dir / (model_id + ".index"))
    , config_path_(model_dir / "config.json")
{}

bool RVCModel::exists() const {
    const bool realtime = std::filesystem::exists(realtime_front_path_)
        && std::filesystem::exists(realtime_decoder_path_);
    return std::filesystem::exists(config_path_) &&
           (std::filesystem::exists(onnx_path_) || std::filesystem::exists(pth_path_)
            || realtime);
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

        const bool has_generator_asset = std::filesystem::exists(onnx_path_)
            || std::filesystem::exists(pth_path_);
        const bool has_realtime_assets = std::filesystem::exists(realtime_front_path_)
            && std::filesystem::exists(realtime_decoder_path_);
        const bool generator_loaded = has_generator_asset
            && load_generator(device, half);
        const bool realtime_loaded = has_realtime_assets
            && load_realtime_generator();
        if (!generator_loaded && !realtime_loaded) {
            spdlog::error("No usable Generator engine found for {}", model_id_);
            return false;
        }

        if (std::filesystem::exists(index_path_)) {
            load_index();
        }

        loaded_ = true;
        spdlog::info(
            "RVC model '{}' loaded (sr={}, emb={}, offline={}, realtime={}) on {}",
            model_id_, config_.sample_rate, config_.emb_channels,
            generator_loaded, realtime_loaded, device);
        return true;

    } catch (const std::exception& e) {
        spdlog::error("Failed to load RVC model {}: {}", model_id_, e.what());
        loaded_ = false;
        return false;
    }
}

void RVCModel::unload() {
    if (generator_engine_) generator_engine_->unload();
    if (realtime_front_engine_) realtime_front_engine_->unload();
    if (realtime_decoder_engine_) realtime_decoder_engine_->unload();
    generator_engine_.reset();
    realtime_front_engine_.reset();
    realtime_decoder_engine_.reset();
    loaded_ = false;
}

bool RVCModel::load_generator(const std::string& device, bool half) {
    if (std::filesystem::exists(onnx_path_)) {
        spdlog::info("Loading generator: {}", onnx_path_.string());
        generator_engine_ = make_engine(onnx_path_);
        return generator_engine_ && generator_engine_->loaded();
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

bool RVCModel::load_realtime_generator() {
    spdlog::info("Loading realtime Generator front: {}", realtime_front_path_.string());
    auto front = make_engine(realtime_front_path_);
    if (!front || !front->loaded()) return false;

    spdlog::info("Loading realtime Generator decoder: {}", realtime_decoder_path_.string());
    auto decoder = make_engine(realtime_decoder_path_);
    if (!decoder || !decoder->loaded()) return false;

    if (!warmup_fixed_engine(
            *front, {"feats", "p_len", "pitch", "sid", "latent_noise"}, "z")
        || !warmup_fixed_engine(
            *decoder, {"z", "pitchf", "sid", "source_phase", "source_noise"},
            "audio")) {
        return false;
    }

    realtime_front_engine_ = std::move(front);
    realtime_decoder_engine_ = std::move(decoder);
    return true;
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
    if (model_id == current_model_id_) {
        const auto current = current_model();
        if (current && current->loaded()) return true;
    }
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
