#include "rvc/inferencer.hpp"
#include <spdlog/spdlog.h>
#include <array>
#include <cmath>
#include <algorithm>
#include <limits>

namespace rvc {

namespace {

constexpr size_t kHop16k = 160;
constexpr size_t kWindow16k = 32000;
constexpr size_t kPad16k = 16000;
constexpr size_t kGeneratorFrames = 200;

std::vector<float> reflect_pad(const std::vector<float>& audio, size_t pad) {
    if (audio.empty() || pad == 0) return audio;
    if (audio.size() == 1) return std::vector<float>(1 + 2 * pad, audio.front());
    const int64_t length = static_cast<int64_t>(audio.size());
    const int64_t period = 2 * (length - 1);
    const auto reflected = [&](int64_t position) {
        position %= period;
        if (position < 0) position += period;
        if (position >= length) position = period - position;
        return static_cast<size_t>(position);
    };
    std::vector<float> out(audio.size() + 2 * pad);
    for (size_t i = 0; i < out.size(); ++i) {
        const int64_t position = static_cast<int64_t>(i) - static_cast<int64_t>(pad);
        out[i] = audio[reflected(position)];
    }
    return out;
}

void peak_normalize(std::vector<float>& audio, float ceiling) {
    float peak = 0.0f;
    for (float sample : audio) peak = std::max(peak, std::fabs(sample));
    const float scale = peak / ceiling;
    if (scale > 1.0f) {
        for (float& sample : audio) sample /= scale;
    }
}

void iir_filter(const std::array<double, 6>& b,
                const std::array<double, 6>& a,
                std::vector<double>& samples,
                double initial_value) {
    // scipy.signal.lfilter_zi(b, a), applied with the same initial endpoint
    // value as scipy.signal.filtfilt's odd-padded input.
    constexpr std::array<double, 5> zi = {
        -0.96996106627015766, 3.879844239395132,
        -5.8197663213460897, 3.8798441895727569,
        -0.96996104135180816
    };
    std::array<double, 5> state{};
    for (size_t i = 0; i < state.size(); ++i) state[i] = zi[i] * initial_value;
    for (double& sample : samples) {
        const double x = sample;
        const double y = b[0] * x + state[0];
        for (size_t i = 1; i < state.size(); ++i) {
            state[i - 1] = b[i] * x - a[i] * y + state[i];
        }
        state.back() = b.back() * x - a.back() * y;
        sample = y;
    }
}

// scipy.signal.butter(5, 48, btype='high', fs=16000) + filtfilt
std::vector<float> highpass_48hz(const std::vector<float>& audio) {
    if (audio.size() < 16) return audio;
    // scipy.signal.butter(5, 48, btype="high", fs=16000).
    // Keep the reference's direct-form coefficients, but use double state;
    // the same recurrence in float32 diverges on ordinary multi-second audio.
    constexpr std::array<double, 6> b = {
        0.96996064518384473, -4.8498032259192234,
        9.6996064518384468, -9.6996064518384468,
        4.8498032259192234, -0.96996064518384473
    };
    constexpr std::array<double, 6> a = {
        1.0, -4.9390018191683636, 9.7578635267395413,
        -9.6395448494134595, 4.7615067973562102,
        -0.94082365320546057
    };
    constexpr size_t pad = 18;
    if (audio.size() <= pad) return audio;
    std::vector<double> ext(audio.size() + 2 * pad);
    for (size_t i = 0; i < pad; ++i) {
        const size_t src = std::min(i + 1, audio.size() - 1);
        ext[pad - 1 - i] = 2.0 * audio[0] - audio[src];
        const size_t rsrc = audio.size() > i + 1 ? audio.size() - 2 - i : 0;
        ext[pad + audio.size() + i] = 2.0 * audio.back() - audio[rsrc];
    }
    for (size_t i = 0; i < audio.size(); ++i) ext[pad + i] = audio[i];

    iir_filter(b, a, ext, ext.front());
    std::reverse(ext.begin(), ext.end());
    iir_filter(b, a, ext, ext.front());
    std::reverse(ext.begin(), ext.end());

    std::vector<float> result(audio.size());
    for (size_t i = 0; i < result.size(); ++i) result[i] = static_cast<float>(ext[pad + i]);
    return result;
}

std::pair<std::vector<int64_t>, std::vector<float>> f0_to_pitch(
    const std::vector<float>& f0, int pitch_shift, size_t frames
) {
    std::vector<float> raw(frames, 0.0f);
    const size_t f0_frames = std::max(size_t{1}, f0.size());
    for (size_t t = 0; t < frames; ++t) {
        if (!f0.empty()) raw[t] = f0[std::min(t, f0_frames - 1)];
    }
    size_t first_voiced = 0;
    while (first_voiced < raw.size() && raw[first_voiced] <= 0.0f) ++first_voiced;
    if (first_voiced < raw.size()) {
        for (size_t t = 0; t < first_voiced; ++t) raw[t] = raw[first_voiced];
        size_t left = first_voiced;
        for (size_t t = first_voiced + 1; t < raw.size(); ++t) {
            if (raw[t] <= 0.0f) continue;
            const size_t right = t;
            for (size_t gap = left + 1; gap < right; ++gap) {
                const float amount = static_cast<float>(gap - left) / static_cast<float>(right - left);
                raw[gap] = raw[left] * (1.0f - amount) + raw[right] * amount;
            }
            left = right;
        }
        for (size_t t = left + 1; t < raw.size(); ++t) raw[t] = raw[left];
    }

    std::vector<int64_t> pitch(frames, 1);
    std::vector<float> pitchf(frames, 0.0f);
    const float shift = std::pow(2.0f, static_cast<float>(pitch_shift) / 12.0f);
    constexpr float f0_min = 50.0f;
    constexpr float f0_max = 1100.0f;
    const float mel_min = 1127.0f * std::log1p(f0_min / 700.0f);
    const float mel_max = 1127.0f * std::log1p(f0_max / 700.0f);
    for (size_t t = 0; t < frames; ++t) {
        pitchf[t] = raw[t] > 0.0f ? raw[t] * shift : 0.0f;
        if (pitchf[t] > 0.0f) {
            const float f0_mel = 1127.0f * std::log1p(pitchf[t] / 700.0f);
            const float coarse = (f0_mel - mel_min) * 254.0f / (mel_max - mel_min) + 1.0f;
            pitch[t] = static_cast<int64_t>(std::clamp(std::lround(coarse), 1L, 255L));
        }
    }
    return {std::move(pitch), std::move(pitchf)};
}

std::vector<float> slice_or_pad(const std::vector<float>& src, size_t start, size_t count) {
    std::vector<float> out(count, 0.0f);
    if (start >= src.size()) return out;
    const size_t take = std::min(count, src.size() - start);
    std::copy_n(src.begin() + static_cast<std::ptrdiff_t>(start), take, out.begin());
    return out;
}

void limit_peak(std::vector<float>& audio, float ceiling) {
    float peak = 0.0f;
    for (float sample : audio) peak = std::max(peak, std::fabs(sample));
    if (peak > ceiling) {
        const float scale = ceiling / peak;
        for (float& sample : audio) sample *= scale;
    }
}

} // namespace

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
    peak_normalize(audio_16k, 0.95f);
    audio_16k = highpass_48hz(audio_16k);

    const uint32_t model_sr = model_->config().sample_rate;
    const size_t hop_out = (kWindow16k / 2) * model_sr / 16000;
    const size_t window_out = kWindow16k * model_sr / 16000;
    const size_t pad_out = kPad16k * model_sr / 16000;

    const auto padded = reflect_pad(audio_16k, kPad16k);
    const auto f0_full = feature_extractor_->extract_f0(padded, 16000, f0_method_);
    auto [pitch_full, pitchf_full] = f0_to_pitch(f0_full, pitch_shift_,
        std::max(f0_full.size(), padded.size() / kHop16k + 1));

    size_t generator_frames = kGeneratorFrames;
    const auto generator_shape = model_->generator_engine().input_shape("feats");
    if (generator_shape.size() >= 2 && generator_shape[1] > 0) {
        generator_frames = static_cast<size_t>(generator_shape[1]);
    }

    // A statically exported full-length Generator can follow the Golden
    // pipeline directly. This avoids changing the model's sequence semantics
    // through 2-second chunking when a matching fixed-length export exists.
    const size_t full_frames = padded.size() / kHop16k;
    if (generator_frames != kGeneratorFrames && generator_frames == full_frames) {
        auto pitchf = slice_or_pad(pitchf_full, 0, generator_frames);
        auto pitch = std::vector<int64_t>(generator_frames, 1);
        for (size_t t = 0; t < generator_frames; ++t) pitch[t] = pitch_full[t];
        auto converted = infer_window(padded, pitchf, pitch);
        const size_t context = kPad16k * model_sr / 16000;
        if (converted.size() > 2 * context) {
            converted = std::vector<float>(
                converted.begin() + static_cast<std::ptrdiff_t>(context),
                converted.end() - static_cast<std::ptrdiff_t>(context));
        }
        converted = apply_rms_mix(audio_16k, std::move(converted));
        if (model_sr != output_sample_rate_) {
            converted = resample(converted, model_sr, output_sample_rate_);
        }
        limit_peak(converted, 0.99f);
        return converted;
    }

    if (audio_16k.size() <= kWindow16k) {
        // A short Golden input is inferred once with two-sided context. Center
        // that source in the fixed T=200 export window and crop the context
        // from the generated waveform before returning it.
        const size_t start = (padded.size() - kWindow16k) / 2;
        const auto window = slice_or_pad(padded, start, kWindow16k);
        const size_t frame_start = start / kHop16k;
        auto pitchf = slice_or_pad(pitchf_full, frame_start, kGeneratorFrames);
        std::vector<int64_t> pitch(kGeneratorFrames, 1);
        for (size_t t = 0; t < kGeneratorFrames; ++t) {
            const size_t source = std::min(frame_start + t, pitch_full.size() - 1);
            pitch[t] = pitch_full[source];
        }
        auto converted = infer_window(window, pitchf, pitch);
        const size_t source_offset = (kPad16k - start) * model_sr / 16000;
        const size_t keep = audio_16k.size() * model_sr / 16000;
        if (source_offset < converted.size()) {
            const size_t count = std::min(keep, converted.size() - source_offset);
            converted = std::vector<float>(
                converted.begin() + static_cast<std::ptrdiff_t>(source_offset),
                converted.begin() + static_cast<std::ptrdiff_t>(source_offset + count));
        } else {
            converted.clear();
        }
        converted = apply_rms_mix(audio_16k, std::move(converted));
        if (model_sr != output_sample_rate_) {
            converted = resample(converted, model_sr, output_sample_rate_);
        }
        limit_peak(converted, 0.99f);
        return converted;
    }

    const size_t hop_in = kWindow16k / 2;
    std::vector<float> assembled;
    assembled.reserve((padded.size() * model_sr / 16000) + window_out);
    size_t windows = 0;
    for (size_t start = 0; start < padded.size(); start += hop_in) {
        auto window = slice_or_pad(padded, start, kWindow16k);
        const size_t f0_start = start / kHop16k;
        std::vector<float> pitchf(kGeneratorFrames, 0.0f);
        std::vector<int64_t> pitch(kGeneratorFrames, 1);
        for (size_t t = 0; t < kGeneratorFrames; ++t) {
            const size_t src = std::min(f0_start + t, pitchf_full.size() - 1);
            pitchf[t] = pitchf_full[src];
            pitch[t] = pitch_full[src];
        }
        auto converted = infer_window(window, pitchf, pitch);
        if (converted.size() < window_out) converted.resize(window_out, 0.0f);
        if (windows == 0) {
            assembled.insert(assembled.end(), converted.begin(),
                             converted.begin() + static_cast<std::ptrdiff_t>(std::min(converted.size(), window_out)));
        } else {
            const size_t overlap = std::min(hop_out, assembled.size());
            const size_t assembled_at = assembled.size() - overlap;
            for (size_t i = 0; i < overlap; ++i) {
                const float w = static_cast<float>(i) / static_cast<float>(std::max(size_t{1}, overlap));
                assembled[assembled_at + i] = assembled[assembled_at + i] * (1.0f - w) + converted[i] * w;
            }
            assembled.insert(assembled.end(),
                             converted.begin() + static_cast<std::ptrdiff_t>(overlap),
                             converted.begin() + static_cast<std::ptrdiff_t>(window_out));
        }
        ++windows;
        if (start + kWindow16k >= padded.size()) break;
    }

    if (assembled.size() > pad_out) {
        assembled.erase(assembled.begin(), assembled.begin() + static_cast<std::ptrdiff_t>(pad_out));
    }
    const size_t expected = audio_16k.size() * model_sr / 16000;
    if (assembled.size() > expected) assembled.resize(expected);

    assembled = apply_rms_mix(audio_16k, std::move(assembled));
    if (model_sr != output_sample_rate_) {
        assembled = resample(assembled, model_sr, output_sample_rate_);
    }
    limit_peak(assembled, 0.99f);
    return assembled;
}

std::vector<float> RVCInferencer::infer_window(
    const std::vector<float>& audio_16k,
    const std::vector<float>& pitchf,
    const std::vector<int64_t>& pitch
) {
    auto feats = feature_extractor_->extract_features(audio_16k, 16000);
    const auto unindexed = feats;
    feats = apply_index(feats);
    return synthesize_window(feats, pitchf, pitch, unindexed);
}

std::vector<float> RVCInferencer::resample(
    const std::vector<float>& audio,
    uint32_t src_sr, uint32_t dst_sr
) {
    if (src_sr == dst_sr || audio.empty()) return audio;

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

std::vector<float> RVCInferencer::synthesize_window(
    const std::vector<float>& feats,
    const std::vector<float>& pitchf,
    const std::vector<int64_t>& pitch,
    const std::vector<float>& unindexed_feats
) {
    if (!model_->generator_engine().loaded()) {
        throw std::runtime_error("Generator engine not loaded");
    }

    uint32_t emb_dim = model_->config().emb_channels;
    size_t T = kGeneratorFrames;
    const auto generator_shape = model_->generator_engine().input_shape("feats");
    if (generator_shape.size() >= 2 && generator_shape[1] > 0) {
        T = static_cast<size_t>(generator_shape[1]);
    }

    const auto resize_features = [&](const std::vector<float>& source_feats) {
        std::vector<float> resized(T * emb_dim, 0.0f);
        const size_t source_frames = std::max(size_t{1}, source_feats.size() / emb_dim);
        for (size_t target = 0; target < T; ++target) {
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
    std::vector<float> pitchf_fixed(T, 0.0f);
    std::vector<int64_t> pitch_fixed(T, 1);
    for (size_t t = 0; t < T; ++t) {
        if (t < pitchf.size()) pitchf_fixed[t] = pitchf[t];
        if (t < pitch.size()) pitch_fixed[t] = pitch[t];
    }

    if (protect_ > 0.0f && protect_ < 1.0f) {
        for (size_t t = 0; t < T; ++t) {
            const float feature_weight = pitchf_fixed[t] > 0.0f ? 1.0f : protect_;
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
    std::vector<float> pitch_float(pitch_fixed.begin(), pitch_fixed.end());

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
        typed_input("pitch", pitch_shape, pitch_float, pitch_fixed),
        typed_input("pitchf", pitchf_shape, pitchf_fixed, {}),
        typed_input("sid", sid_shape, sid_float, sid_int64)
    };
    return model_->generator_engine().run(inputs, {"audio"});
}

std::vector<float> RVCInferencer::apply_rms_mix(
    const std::vector<float>& original_16k,
    std::vector<float> audio_out
) {
    if (rms_mix_rate_ >= 1.0f || original_16k.empty() || audio_out.empty()) {
        return audio_out;
    }
    const auto rms_curve = [](const std::vector<float>& samples, uint32_t sample_rate) {
        const size_t frame_size = std::max<size_t>(1, sample_rate);
        const size_t hop_size = std::max<size_t>(1, sample_rate / 2);
        const size_t center_pad = frame_size / 2;
        std::vector<float> result;
        for (size_t center = 0; center <= samples.size(); center += hop_size) {
            const size_t start = center > center_pad ? center - center_pad : 0;
            const size_t end = std::min(samples.size(), center + center_pad);
            float sum = 0.0f;
            for (size_t i = start; i < end; ++i) sum += samples[i] * samples[i];
            result.push_back(std::sqrt(sum / static_cast<float>(frame_size)));
        }
        return result;
    };
    const auto input_rms = rms_curve(original_16k, 16000);
    const auto output_rms = rms_curve(audio_out, model_->config().sample_rate);
    const auto interpolate = [&](const std::vector<float>& curve, size_t sample) {
        if (curve.size() == 1) return curve.front();
        const float source = (static_cast<float>(sample) + 0.5f)
            * static_cast<float>(curve.size()) / static_cast<float>(audio_out.size()) - 0.5f;
        const float clamped = std::clamp(source, 0.0f, static_cast<float>(curve.size() - 1));
        const size_t left = static_cast<size_t>(clamped);
        const size_t right = std::min(left + 1, curve.size() - 1);
        const float amount = clamped - static_cast<float>(left);
        return curve[left] * (1.0f - amount) + curve[right] * amount;
    };
    for (size_t i = 0; i < audio_out.size(); ++i) {
        const float in_level = interpolate(input_rms, i);
        const float out_level = std::max(interpolate(output_rms, i), 1e-6f);
        const float correction = std::pow(std::max(in_level, 1e-6f) / out_level,
                                          1.0f - rms_mix_rate_);
        audio_out[i] *= correction;
    }
    return audio_out;
}

} // namespace rvc
