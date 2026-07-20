#include "rvc/feature_extractor.hpp"
#include <spdlog/spdlog.h>
#include <fstream>
#include <cmath>
#include <complex>
#include <numbers>

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
    if (std::filesystem::exists(hubert_path_)) {
        bool ok = hubert_engine_.load(hubert_path_);
        if (ok) {
            spdlog::info("HuBERT engine loaded: {}", hubert_path_.string());
        }
    } else {
        spdlog::warn("HuBERT model not found at {}", hubert_path_.string());
    }

    if (rmvpe_path_ && std::filesystem::exists(*rmvpe_path_)) {
        bool ok = rmvpe_engine_.load(*rmvpe_path_);
        if (ok) {
            spdlog::info("RMVPE engine loaded: {}", rmvpe_path_->string());
        }
    } else if (rmvpe_path_) {
        spdlog::warn("RMVPE path not found: {}", rmvpe_path_->string());
    }
}

FeatureExtractor::~FeatureExtractor() = default;

std::vector<float> FeatureExtractor::compute_mel(
    const std::vector<float>& audio,
    uint32_t sample_rate,
    int n_mels, int n_fft, int hop_length
) {
    size_t n_samples = audio.size();
    size_t n_frames = (n_samples - n_fft) / hop_length + 1;
    if (n_frames < 1) n_frames = 1;

    std::vector<float> mel(n_frames * n_mels, 0.0f);

    float f_min = 0.0f;
    float f_max = sample_rate / 2.0f;
    float mel_min = 1127.0f * std::log1p(f_min / 700.0f);
    float mel_max = 1127.0f * std::log1p(f_max / 700.0f);

    for (size_t frame = 0; frame < n_frames; ++frame) {
        float energy = 0.0f;
        size_t offset = frame * hop_length;
        for (int i = 0; i < n_fft && (offset + i) < n_samples; ++i) {
            energy += audio[offset + i] * audio[offset + i];
        }
        energy = energy / n_fft;

        for (int m = 0; m < n_mels; ++m) {
            float mel_f = mel_min + (mel_max - mel_min) * m / (n_mels - 1);
            float hz = 700.0f * (std::exp(mel_f / 1127.0f) - 1.0f);
            int bin = static_cast<int>(hz / sample_rate * n_fft);
            if (bin < n_fft / 2) {
                mel[frame * n_mels + m] = energy * std::exp(-0.5f * static_cast<float>(bin));
            }
        }
    }

    return mel;
}

std::vector<float> FeatureExtractor::extract_f0(
    const std::vector<float>& audio,
    uint32_t sample_rate,
    const std::string& method
) {
    if (method == "rmvpe" && rmvpe_engine_.loaded()) {
        auto mel = compute_mel(audio, sample_rate);
        size_t n_frames = mel.size() / 128;
        if (n_frames < 1) n_frames = 1;

        std::vector<int64_t> mel_shape = {1, static_cast<int64_t>(n_frames), 128};
        auto f0 = rmvpe_engine_.run(
            {"mel"}, {mel_shape}, {mel}, {"f0"}
        );

        spdlog::debug("RMVPE F0: {} frames extracted", f0.size());
        return f0;
    }

    if (method == "harvest") {
        return f0_harvest(audio, sample_rate);
    }

    if (method == "pm") {
        return f0_pm(audio, sample_rate);
    }

    spdlog::warn("F0 extractor unavailable; returning zero F0");
    size_t n_frames = audio.size() / 512;
    if (n_frames == 0) n_frames = 1;
    return std::vector<float>(n_frames, 0.0f);
}

std::vector<float> FeatureExtractor::extract_features(
    const std::vector<float>& audio,
    uint32_t sample_rate
) {
    if (hubert_engine_.loaded()) {
        size_t n_samples = audio.size();
        std::vector<int64_t> audio_shape = {1, static_cast<int64_t>(n_samples)};
        auto feats = hubert_engine_.run(
            {"audio"}, {audio_shape}, {audio}, {"features"}
        );
        spdlog::debug("HuBERT features: {} elements extracted", feats.size());
        return feats;
    }

    spdlog::warn("HuBERT model unavailable; returning dummy features");
    size_t n_frames = audio.size() / 512;
    if (n_frames == 0) n_frames = 1;
    return std::vector<float>(n_frames * 768, 0.0f);
}

std::vector<float> FeatureExtractor::f0_harvest(
    const std::vector<float>& audio,
    uint32_t sample_rate
) {
    spdlog::warn("pyworld harvest not available; using zero F0");
    size_t n_frames = audio.size() / 512;
    if (n_frames == 0) n_frames = 1;
    return std::vector<float>(n_frames, 0.0f);
}

std::vector<float> FeatureExtractor::f0_pm(
    const std::vector<float>& audio,
    uint32_t sample_rate
) {
    spdlog::warn("parselmouth pm not available; using zero F0");
    size_t n_frames = audio.size() / 512;
    if (n_frames == 0) n_frames = 1;
    return std::vector<float>(n_frames, 0.0f);
}

} // namespace rvc
