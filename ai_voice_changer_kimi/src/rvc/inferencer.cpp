#include "rvc/inferencer.hpp"
#include <spdlog/spdlog.h>
#include <cmath>

namespace rvc {

RVCInferencer::RVCInferencer(
    std::shared_ptr<RVCModel> model,
    std::shared_ptr<FeatureExtractor> feature_extractor,
    uint32_t input_sample_rate,
    uint32_t output_sample_rate,
    int pitch_shift,
    float index_rate,
    int filter_radius,
    float rms_mix_rate,
    float protect
)
    : model_(model)
    , feature_extractor_(feature_extractor)
    , input_sample_rate_(input_sample_rate)
    , output_sample_rate_(output_sample_rate)
    , pitch_shift_(pitch_shift)
    , index_rate_(index_rate)
    , filter_radius_(filter_radius)
    , rms_mix_rate_(rms_mix_rate)
    , protect_(protect)
{}

std::vector<float> RVCInferencer::infer(const std::vector<float>& audio) {
    if (!model_->loaded()) {
        throw std::runtime_error("RVC model not loaded");
    }

    // 1. If input is not 16 kHz, resample to 16 kHz for feature extraction
    std::vector<float> audio_16k;
    if (input_sample_rate_ != 16000) {
        audio_16k = resample(audio, input_sample_rate_, 16000);
    } else {
        audio_16k = audio;
    }

    // 2. Extract F0 and content features
    auto f0 = feature_extractor_->extract_f0(audio_16k, 16000, "rmvpe");
    auto feats = feature_extractor_->extract_features(audio_16k, 16000);

    // 3. Feature retrieval (index search)
    feats = apply_index(feats);

    // 4. Run generator (placeholder)
    auto converted = run_generator(feats, f0, audio);

    return converted;
}

std::vector<float> RVCInferencer::resample(
    const std::vector<float>& audio,
    uint32_t src_sr, uint32_t dst_sr
) {
    if (src_sr == dst_sr) return audio;

    float ratio = static_cast<float>(dst_sr) / static_cast<float>(src_sr);
    size_t n_out = static_cast<size_t>(static_cast<float>(audio.size()) * ratio);
    std::vector<float> result(n_out);

    for (size_t i = 0; i < n_out; ++i) {
        float src_idx = static_cast<float>(i) / ratio;
        size_t idx0 = static_cast<size_t>(src_idx);
        if (idx0 + 1 >= audio.size()) {
            result[i] = audio.back();
        } else {
            float frac = src_idx - static_cast<float>(idx0);
            result[i] = audio[idx0] * (1.0f - frac) + audio[idx0 + 1] * frac;
        }
    }
    return result;
}

std::vector<float> RVCInferencer::apply_index(const std::vector<float>& feats) {
    // TODO: implement FAISS index search when model has index loaded
    // For now, return features unchanged (index_rate affects mix ratio)
    spdlog::debug("Index search not implemented in Phase 1; returning original features");
    return feats;
}

std::vector<float> RVCInferencer::run_generator(
    const std::vector<float>& feats,
    const std::vector<float>& f0,
    const std::vector<float>& original_audio
) {
    // TODO: Replace with real RVC generator forward pass.
    // This requires libtorch integration with actual RVC model classes.
    //
    // Expected shapes:
    //   feats:  [1, T, 768] flattened
    //   pitch:  [1, T]
    //   pitchf: [1, T]
    //   lengths: scalar T
    //   sid:    [1]
    //
    // For Phase 1 skeleton: upsample the original audio to output_sample_rate_

    spdlog::warn("Real generator inference not implemented; upsampling input to {}Hz",
                 output_sample_rate_);

    if (input_sample_rate_ == output_sample_rate_) {
        return original_audio;
    }

    // Simple upsample by integer ratio (e.g. 16k -> 48k = 3x)
    if (output_sample_rate_ % input_sample_rate_ == 0) {
        int ratio = output_sample_rate_ / input_sample_rate_;
        std::vector<float> result;
        result.reserve(original_audio.size() * static_cast<size_t>(ratio));
        for (float s : original_audio) {
            for (int i = 0; i < ratio; ++i) {
                result.push_back(s);
            }
        }
        return result;
    }

    return resample(original_audio, input_sample_rate_, output_sample_rate_);
}

} // namespace rvc
