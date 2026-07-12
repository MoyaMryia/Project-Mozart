#pragma once

#include <cstdint>
#include <vector>
#include <memory>
#include <string>
#include <filesystem>

namespace rvc {

// ──────────────────────────────────────────────────────────
// RVC Feature Extractor (HuBERT + RMVPE / harvest / pm)
// ──────────────────────────────────────────────────────────
class FeatureExtractor {
public:
    FeatureExtractor(
        const std::filesystem::path& hubert_path,
        const std::optional<std::filesystem::path>& rmvpe_path,
        const std::string& device = "cuda",
        bool half = false
    );

    ~FeatureExtractor();

    // Extract F0 (pitch) contour. Audio must be 16 kHz mono float32.
    std::vector<float> extract_f0(
        const std::vector<float>& audio,
        uint32_t sample_rate = 16000,
        const std::string& method = "rmvpe"
    );

    // Extract content features (HuBERT). Returns [1, T, 768] as flattened.
    // Audio must be 16 kHz mono float32.
    std::vector<float> extract_features(
        const std::vector<float>& audio,
        uint32_t sample_rate = 16000
    );

    bool is_hubert_loaded() const { return hubert_loaded_; }

private:
    std::filesystem::path hubert_path_;
    std::optional<std::filesystem::path> rmvpe_path_;
    std::string device_;
    bool half_;
    bool hubert_loaded_ = false;

    // Placeholder: torch::jit::Module hubert_model_;

    std::vector<float> f0_harvest(const std::vector<float>& audio, uint32_t sample_rate);
    std::vector<float> f0_pm(const std::vector<float>& audio, uint32_t sample_rate);
};

} // namespace rvc
