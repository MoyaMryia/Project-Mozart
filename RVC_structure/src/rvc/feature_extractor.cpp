#include "rvc/feature_extractor.hpp"
#include <spdlog/spdlog.h>
#include <fstream>
#include <cmath>

namespace rvc {

FeatureExtractor::FeatureExtractor(
    const std::filesystem::path& hubert_path,
    const std::optional<std::filesystem::path>& rmvpe_path,
    const std::string& device,
    bool half
)
    : hubert_path_(hubert_path)
    , rmvpe_path_(rmvpe_path)
    , device_(device)
    , half_(half)
{
    if (!std::filesystem::exists(hubert_path_)) {
        spdlog::warn("HuBERT model not found at {}", hubert_path_.string());
        return;
    }

    // TODO: Load HuBERT model via libtorch torch::jit::load
    // Placeholder for Phase 1
    spdlog::info("HuBERT model loading stub (Phase 1): {}", hubert_path_.string());
    hubert_loaded_ = true; // Pretend loaded for skeleton testing

    if (rmvpe_path_ && std::filesystem::exists(*rmvpe_path_)) {
        spdlog::info("RMVPE path found: {}", rmvpe_path_->string());
        // TODO: Load RMVPE model
    } else {
        spdlog::warn("RMVPE path not provided; F0 extraction will use fallback");
    }
}

std::vector<float> FeatureExtractor::extract_f0(
    const std::vector<float>& audio,
    uint32_t sample_rate,
    const std::string& method
) {
    if (method == "rmvpe" && rmvpe_path_) {
        // TODO: RMVPE inference
        spdlog::debug("RMVPE F0 extraction not implemented in Phase 1");
    }

    if (method == "harvest") {
        return f0_harvest(audio, sample_rate);
    }

    if (method == "pm") {
        return f0_pm(audio, sample_rate);
    }

    // Fallback: zero F0 (unvoiced)
    spdlog::warn("F0 extractor unavailable; returning zero F0");
    size_t n_frames = audio.size() / 512;
    if (n_frames == 0) n_frames = 1;
    return std::vector<float>(n_frames, 0.0f);
}

std::vector<float> FeatureExtractor::extract_features(
    const std::vector<float>& audio,
    uint32_t sample_rate
) {
    if (!hubert_loaded_) {
        spdlog::warn("HuBERT model unavailable; returning dummy features");
        size_t n_frames = audio.size() / 512;
        if (n_frames == 0) n_frames = 1;
        return std::vector<float>(n_frames * 768, 0.0f); // [1, T, 768] flattened
    }

    // TODO: Run HuBERT model via libtorch
    // Placeholder for Phase 1
    size_t n_frames = audio.size() / 512;
    if (n_frames == 0) n_frames = 1;
    return std::vector<float>(n_frames * 768, 0.0f);
}

std::vector<float> FeatureExtractor::f0_harvest(
    const std::vector<float>& audio,
    uint32_t sample_rate
) {
    // TODO: integrate pyworld or equivalent C++ pitch extractor
    spdlog::warn("pyworld harvest not available in C++ build; using zero F0");
    size_t n_frames = audio.size() / 512;
    if (n_frames == 0) n_frames = 1;
    return std::vector<float>(n_frames, 0.0f);
}

std::vector<float> FeatureExtractor::f0_pm(
    const std::vector<float>& audio,
    uint32_t sample_rate
) {
    // TODO: integrate parselmouth or equivalent C++ pitch extractor
    spdlog::warn("parselmouth pm not available in C++ build; using zero F0");
    size_t n_frames = audio.size() / 512;
    if (n_frames == 0) n_frames = 1;
    return std::vector<float>(n_frames, 0.0f);
}

FeatureExtractor::~FeatureExtractor() = default;

} // namespace rvc
