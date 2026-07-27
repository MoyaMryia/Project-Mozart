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
    std::string f0_method,
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
    , f0_method_(std::move(f0_method))
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

    auto f0 = feature_extractor_->extract_f0(audio_16k, 16000, f0_method_);
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
    const size_t source_feature_frames = std::max(size_t{1}, feats.size() / emb_dim);
    // Current deployed generator export only supports its tracing length T=200.
    constexpr size_t generator_frames = 200;
    const size_t T = generator_frames;

    std::vector<float> feats_reshaped(T * emb_dim, 0.0f);
    for (size_t target = 0; target < T; ++target) {
        const size_t source = source_feature_frames == 1 ? 0
            : target * (source_feature_frames - 1) / (T - 1);
        const size_t source_offset = source * emb_dim;
        if (source_offset >= feats.size()) continue;
        const size_t available = std::min(static_cast<size_t>(emb_dim), feats.size() - source_offset);
        std::copy_n(feats.begin() + static_cast<std::ptrdiff_t>(source_offset), available,
                    feats_reshaped.begin() + static_cast<std::ptrdiff_t>(target * emb_dim));
    }

    const size_t f0_frames = std::max(size_t{1}, f0.size());
    std::vector<int64_t> pitch(T, 0);
    std::vector<float> pitchf(T, 0.0f);

    for (size_t t = 0; t < T; ++t) {
        if (!f0.empty()) {
            const size_t source = f0_frames == 1 ? 0 : t * (f0_frames - 1) / (T - 1);
            float f0_val = f0[source];
            pitch[t] = f0_val > 0.0f
                ? static_cast<int64_t>(std::clamp(std::lround(std::log2(f0_val / 440.0f) * 12.0f + 69.0f + pitch_shift_), 1L, 255L))
                : 0;
            pitchf[t] = f0_val;
        }
    }

    std::vector<int64_t> sid_int64 = {model_->config().spk_id};
    std::vector<int64_t> p_len_int64 = {static_cast<int64_t>(T)};
    std::vector<float> sid_float = {static_cast<float>(model_->config().spk_id)};
    std::vector<float> p_len_float = {static_cast<float>(T)};
    std::vector<float> pitch_float(pitch.begin(), pitch.end());

    std::vector<int64_t> feats_shape = {1, static_cast<int64_t>(T),
                                         static_cast<int64_t>(emb_dim)};
    std::vector<int64_t> p_len_shape = {1};
    std::vector<int64_t> pitch_shape = {1, static_cast<int64_t>(T)};
    std::vector<int64_t> pitchf_shape = {1, static_cast<int64_t>(T)};
    std::vector<int64_t> sid_shape = {1};

    const auto typed_input = [&](const char* name, const std::vector<int64_t>& shape,
                                 std::vector<float> floats, std::vector<int64_t> int64s) {
        const auto type = model_->generator_engine().input_type(name).value_or(OnnxInput::Type::Float);
        return OnnxInput{name, shape, type, std::move(floats), std::move(int64s)};
    };
    const std::vector<OnnxInput> inputs = {
        typed_input("feats", feats_shape, feats_reshaped, {}),
        typed_input("p_len", p_len_shape, p_len_float, p_len_int64),
        typed_input("pitch", pitch_shape, pitch_float, pitch),
        typed_input("pitchf", pitchf_shape, pitchf, {}),
        typed_input("sid", sid_shape, sid_float, sid_int64)
    };
    auto audio_out = model_->generator_engine().run(inputs, {"audio"});

    if (model_->config().sample_rate != output_sample_rate_) {
        audio_out = resample(audio_out, model_->config().sample_rate, output_sample_rate_);
    }

    // The generator output is at output_sample_rate_, while original_audio is
    // at input_sample_rate_. Mixing them without resampling compresses the
    // preserved consonants into the start of the output timeline.
    if (protect_ > 0.0f) {
        const auto original_at_output_rate = input_sample_rate_ == output_sample_rate_
            ? original_audio
            : resample(original_audio, input_sample_rate_, output_sample_rate_);
        float alpha = 1.0f - protect_ * 0.5f;
        size_t limit = std::min(audio_out.size(), original_at_output_rate.size());
        for (size_t i = 0; i < limit; ++i) {
            audio_out[i] = audio_out[i] * alpha + original_at_output_rate[i] * (1.0f - alpha);
        }
    }

    spdlog::debug("Generator output: {} samples @ {}Hz", audio_out.size(),
                  model_->config().sample_rate);
    return audio_out;
}

} // namespace rvc
