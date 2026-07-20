#include "rvc/inferencer.hpp"
#include <spdlog/spdlog.h>
#include <cmath>
#include <algorithm>

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

    std::vector<float> audio_16k;
    if (input_sample_rate_ != 16000) {
        audio_16k = resample(audio, input_sample_rate_, 16000);
    } else {
        audio_16k = audio;
    }

    auto f0 = feature_extractor_->extract_f0(audio_16k, 16000, "rmvpe");
    auto feats = feature_extractor_->extract_features(audio_16k, 16000);

    feats = apply_index(feats);

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
    if (!model_->index().loaded() || index_rate_ <= 0.0f) {
        return feats;
    }

    return model_->index().search(
        feats,
        model_->config().emb_channels,
        index_rate_
    );
}

std::vector<float> RVCInferencer::run_generator(
    const std::vector<float>& feats,
    const std::vector<float>& f0,
    const std::vector<float>& original_audio
) {
    if (!model_->generator_engine().loaded()) {
        spdlog::warn("Generator engine not loaded; falling back to upsampling");
        if (input_sample_rate_ == output_sample_rate_) {
            return original_audio;
        }
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

    uint32_t emb_dim = model_->config().emb_channels;
    size_t total_elems = feats.size();
    size_t T = total_elems / emb_dim;
    if (T < 1) T = 1;

    std::vector<float> feats_reshaped = feats;
    feats_reshaped.resize(T * emb_dim, 0.0f);

    size_t f0_frames = f0.size();
    std::vector<float> pitch(T, 0.0f);
    std::vector<float> pitchf(T, 0.0f);

    for (size_t t = 0; t < T; ++t) {
        if (t < f0_frames) {
            float f0_val = f0[t];
            pitch[t] = f0_val > 0.0f ? std::log2(f0_val / 440.0f) * 12.0f + 69.0f : 0.0f;
            pitchf[t] = f0_val;
        }
    }

    float sid_val = static_cast<float>(model_->config().spk_id);

    std::vector<float> sid_vec = {sid_val};
    std::vector<float> p_len_vec = {static_cast<float>(T)};

    std::vector<int64_t> feats_shape = {1, static_cast<int64_t>(T),
                                         static_cast<int64_t>(emb_dim)};
    std::vector<int64_t> p_len_shape = {1};
    std::vector<int64_t> pitch_shape = {1, static_cast<int64_t>(T)};
    std::vector<int64_t> pitchf_shape = {1, static_cast<int64_t>(T)};
    std::vector<int64_t> sid_shape = {1};

    std::vector<const char*> input_names = {"feats", "p_len", "pitch", "pitchf", "sid"};
    std::vector<std::vector<int64_t>> input_shapes = {
        feats_shape, p_len_shape, pitch_shape, pitchf_shape, sid_shape
    };
    std::vector<std::vector<float>> input_data = {
        feats_reshaped, p_len_vec, pitch, pitchf, sid_vec
    };

    auto audio_out = model_->generator_engine().run(
        input_names, input_shapes, input_data, {"audio"}
    );

    if (model_->config().sample_rate != output_sample_rate_) {
        audio_out = resample(audio_out, model_->config().sample_rate, output_sample_rate_);
    }

    // Mix with original for voice preservation
    if (protect_ > 0.0f && audio_out.size() >= original_audio.size()) {
        float alpha = 1.0f - protect_ * 0.5f;
        size_t limit = std::min(audio_out.size(), original_audio.size());
        for (size_t i = 0; i < limit; ++i) {
            audio_out[i] = audio_out[i] * alpha + original_audio[i] * (1.0f - alpha);
        }
    }

    spdlog::debug("Generator output: {} samples @ {}Hz", audio_out.size(),
                  model_->config().sample_rate);
    return audio_out;
}

} // namespace rvc
