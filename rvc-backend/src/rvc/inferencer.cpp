#include "rvc/inferencer.hpp"
#include <spdlog/spdlog.h>
#include <cmath>
#include <algorithm>
#include <limits>

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
    const auto unindexed_feats = feats;

    feats = apply_index(feats);

    auto converted = run_generator(feats, f0, audio, unindexed_feats);

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
    const std::vector<float>& original_audio,
    const std::vector<float>& unindexed_feats
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
    // Current deployed generator export only supports its tracing length T=200.
    constexpr size_t generator_frames = 200;
    const size_t T = generator_frames;

    const auto resize_features = [&](const std::vector<float>& source_feats) {
        std::vector<float> resized(T * emb_dim, 0.0f);
        const size_t source_frames = std::max(size_t{1}, source_feats.size() / emb_dim);
        for (size_t target = 0; target < T; ++target) {
            // RVC uses F.interpolate(..., scale_factor=2) with the default
            // nearest mode. A 2 s HuBERT window yields 99 frames, so duplicate
            // those to 198 and extend the final frame to the fixed T=200 export.
            const size_t source = std::min(target / 2, source_frames - 1);
            for (size_t channel = 0; channel < emb_dim; ++channel) {
                const size_t source_offset = source * emb_dim + channel;
                if (source_offset < source_feats.size()) {
                    resized[target * emb_dim + channel] = source_feats[source_offset];
                }
            }
        }
        return resized;
    };

    std::vector<float> feats_reshaped = resize_features(feats);
    const std::vector<float> unindexed_reshaped = resize_features(unindexed_feats);

    const size_t f0_frames = std::max(size_t{1}, f0.size());
    std::vector<int64_t> pitch(T, 0);
    std::vector<float> pitchf(T, 0.0f);

    std::vector<float> raw_f0(T, 0.0f);
    for (size_t t = 0; t < T; ++t) {
        if (!f0.empty()) {
            raw_f0[t] = f0[std::min(t, f0_frames - 1)];
        }
    }
    // RVC fills unvoiced gaps before applying the requested pitch shift. This
    // keeps consonants from forcing the generator to jump to coarse bin 1.
    size_t first_voiced = 0;
    while (first_voiced < raw_f0.size() && raw_f0[first_voiced] <= 0.0f) ++first_voiced;
    if (first_voiced < raw_f0.size()) {
        for (size_t t = 0; t < first_voiced; ++t) raw_f0[t] = raw_f0[first_voiced];
        size_t left = first_voiced;
        for (size_t t = first_voiced + 1; t < raw_f0.size(); ++t) {
            if (raw_f0[t] <= 0.0f) continue;
            const size_t right = t;
            for (size_t gap = left + 1; gap < right; ++gap) {
                const float amount = static_cast<float>(gap - left) / static_cast<float>(right - left);
                raw_f0[gap] = raw_f0[left] * (1.0f - amount) + raw_f0[right] * amount;
            }
            left = right;
        }
        for (size_t t = left + 1; t < raw_f0.size(); ++t) raw_f0[t] = raw_f0[left];
    }

    std::vector<float> shifted_f0(T, 0.0f);
    for (size_t t = 0; t < T; ++t) {
        shifted_f0[t] = raw_f0[t] > 0.0f
            ? raw_f0[t] * std::pow(2.0f, static_cast<float>(pitch_shift_) / 12.0f)
            : 0.0f;
        pitchf[t] = shifted_f0[t];
        if (shifted_f0[t] > 0.0f) {
                constexpr float f0_min = 50.0f;
                constexpr float f0_max = 1100.0f;
                const float mel_min = 1127.0f * std::log1p(f0_min / 700.0f);
                const float mel_max = 1127.0f * std::log1p(f0_max / 700.0f);
                const float f0_mel = 1127.0f * std::log1p(shifted_f0[t] / 700.0f);
                const float coarse = (f0_mel - mel_min) * 254.0f / (mel_max - mel_min) + 1.0f;
                pitch[t] = static_cast<int64_t>(std::clamp(std::lround(coarse), 1L, 255L));
            } else {
                pitch[t] = 1;
            }
    }

    // RVC protect keeps the original HuBERT features around unvoiced consonants;
    // it is not a waveform crossfade with the source audio.
    if (protect_ > 0.0f && protect_ < 1.0f) {
        for (size_t t = 0; t < T; ++t) {
            const float feature_weight = shifted_f0[t] > 0.0f ? 1.0f : protect_;
            for (size_t channel = 0; channel < emb_dim; ++channel) {
                const size_t offset = t * emb_dim + channel;
                feats_reshaped[offset] = feats_reshaped[offset] * feature_weight
                    + unindexed_reshaped[offset] * (1.0f - feature_weight);
            }
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

    if (rms_mix_rate_ < 1.0f && !original_audio.empty() && !audio_out.empty()) {
        const auto input_at_output_rate = input_sample_rate_ == output_sample_rate_
            ? original_audio : resample(original_audio, input_sample_rate_, output_sample_rate_);
        const size_t frame = std::max<size_t>(1, output_sample_rate_ / 2);
        const size_t hop = std::max<size_t>(1, output_sample_rate_ / 4);
        const auto rms_curve = [](const std::vector<float>& samples, size_t frame_size,
                                  size_t hop_size) {
            std::vector<float> result;
            for (size_t start = 0; start < samples.size(); start += hop_size) {
                const size_t end = std::min(samples.size(), start + frame_size);
                float sum = 0.0f;
                for (size_t i = start; i < end; ++i) sum += samples[i] * samples[i];
                result.push_back(std::sqrt(sum / static_cast<float>(std::max<size_t>(1, end - start))));
            }
            return result;
        };
        const auto input_rms = rms_curve(input_at_output_rate, frame, hop);
        const auto output_rms = rms_curve(audio_out, frame, hop);
        for (size_t i = 0; i < audio_out.size(); ++i) {
            const float position = static_cast<float>(i) / static_cast<float>(hop);
            const size_t left = std::min(static_cast<size_t>(position), output_rms.size() - 1);
            const size_t right = std::min(left + 1, output_rms.size() - 1);
            const float amount = position - static_cast<float>(left);
            const float in_level = input_rms[std::min(left, input_rms.size() - 1)] * (1.0f - amount)
                + input_rms[std::min(right, input_rms.size() - 1)] * amount;
            const float out_level = std::max(output_rms[left] * (1.0f - amount)
                + output_rms[right] * amount, 1e-6f);
            const float correction = std::pow(std::max(in_level, 1e-6f) / out_level,
                                              1.0f - rms_mix_rate_);
            audio_out[i] *= correction;
        }
    }

    spdlog::debug("Generator output: {} samples @ {}Hz", audio_out.size(),
                  model_->config().sample_rate);
    return audio_out;
}

} // namespace rvc
