#include "rvc/feature_extractor.hpp"
#include "rvc/trt_engine.hpp"

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
    bool half,
    bool mock_hubert,
    bool mock_rmvpe
)
    : hubert_path_(hubert_path)
    , rmvpe_path_(rmvpe_path)
    , device_(device)
    , half_(half)
    , mock_hubert_(mock_hubert)
    , mock_rmvpe_(mock_rmvpe)
{
    if (std::filesystem::exists(hubert_path_)) {
        hubert_engine_ = make_engine(hubert_path_);
        // TRT 引擎是固定形状：HuBERT 必须接受我们的整窗长度（流式 2s=32000）
        if (auto* trt = dynamic_cast<TrtEngine*>(hubert_engine_.get())) {
            const auto shape = trt->input_shape("audio");
            const int64_t need = 32000; // 流式窗口 2s @16k
            if (!shape.empty() && (shape.size() != 2 || shape[1] != need)) {
                spdlog::warn("HuBERT TRT 引擎形状不符（audio={}[{}] ≠ [1,{}]），回退 ONNX",
                             shape.size() >= 2 ? shape[1] : -1, shape.size(), need);
                hubert_engine_.reset();
            }
        }
        if (hubert_engine_ && hubert_engine_->loaded()) {
            spdlog::info("HuBERT engine loaded: {}", hubert_path_.string());
        }
    } else {
        spdlog::warn("HuBERT model not found at {}", hubert_path_.string());
    }

    if (rmvpe_path_ && std::filesystem::exists(*rmvpe_path_)) {
        rmvpe_engine_ = make_engine(*rmvpe_path_);
        // RMVPE 固定 [1,128,128]（extract_f0 会零填充到该形状）
        if (auto* trt = dynamic_cast<TrtEngine*>(rmvpe_engine_.get())) {
            const auto shape = trt->input_shape("mel");
            if (!shape.empty() && shape != std::vector<int64_t>({1, 128, 128})) {
                spdlog::warn("RMVPE TRT 引擎形状不符，回退 ONNX");
                rmvpe_engine_.reset();
            }
        }
        if (rmvpe_engine_ && rmvpe_engine_->loaded()) {
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
    const size_t n_samples = audio.size();
    const size_t n_frames = n_samples <= static_cast<size_t>(n_fft)
        ? 1 : (n_samples - static_cast<size_t>(n_fft)) / static_cast<size_t>(hop_length) + 1;

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
    if (method == "rmvpe" && rmvpe_engine_ && rmvpe_engine_->loaded()) {
        auto mel = compute_mel(audio, sample_rate);
        // export_rmvpe_onnx.py exports the model with a fixed [1, 128, 128]
        // mel tensor. Preserve short-frame support by zero-padding the tail.
        constexpr size_t rmvpe_frames = 128;
        constexpr size_t mel_bins = 128;
        std::vector<float> fixed_mel(rmvpe_frames * mel_bins, 0.0f);
        std::copy_n(mel.begin(), std::min(mel.size(), fixed_mel.size()), fixed_mel.begin());
        const std::vector<int64_t> mel_shape = {1, static_cast<int64_t>(rmvpe_frames), static_cast<int64_t>(mel_bins)};
        auto f0 = rmvpe_engine_->run(
            {"mel"}, {mel_shape}, {fixed_mel}, {"f0"}
        );
        // RMVPE emits [1, 128, 360] logits. Convert the dominant pitch bin to
        // Hertz; bin 0 represents unvoiced audio.
        constexpr size_t output_frames = 128;
        constexpr size_t pitch_bins = 360;
        std::vector<float> hz(output_frames, 0.0f);
        if (f0.size() >= output_frames * pitch_bins) {
            for (size_t frame = 0; frame < output_frames; ++frame) {
                const auto begin = f0.begin() + static_cast<std::ptrdiff_t>(frame * pitch_bins);
                const auto maximum = std::max_element(begin, begin + static_cast<std::ptrdiff_t>(pitch_bins));
                const size_t bin = static_cast<size_t>(maximum - begin);
                if (bin > 0) hz[frame] = 10.0f * std::pow(2.0f, static_cast<float>(bin) / 60.0f);
            }
        }
        spdlog::debug("RMVPE F0: {} frames extracted", hz.size());
        return hz;
    }

    if (method == "harvest") {
        return f0_harvest(audio, sample_rate);
    }

    if (method == "pm") {
        return f0_pm(audio, sample_rate);
    }

    if (!mock_rmvpe_) {
        throw std::runtime_error("RMVPE engine is unavailable and rvc.mock.rmvpe is false");
    }
    spdlog::warn("RMVPE mock enabled; returning zero F0");
    size_t n_frames = audio.size() / 512;
    if (n_frames == 0) n_frames = 1;
    return std::vector<float>(n_frames, 0.0f);
}

std::vector<float> FeatureExtractor::extract_features(
    const std::vector<float>& audio,
    uint32_t sample_rate
) {
    if (hubert_engine_ && hubert_engine_->loaded()) {
        size_t n_samples = audio.size();
        std::vector<int64_t> audio_shape = {1, static_cast<int64_t>(n_samples)};
        auto feats = hubert_engine_->run(
            {"audio"}, {audio_shape}, {audio}, {"features"}
        );
        spdlog::debug("HuBERT features: {} elements extracted", feats.size());
        return feats;
    }

    if (!mock_hubert_) {
        throw std::runtime_error("HuBERT engine is unavailable and rvc.mock.hubert is false");
    }
    spdlog::warn("HuBERT mock enabled; returning dummy features");
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
