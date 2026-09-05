#include "rvc/inferencer.hpp"
#include "rvc/profile.hpp"
#include <spdlog/spdlog.h>
#include <array>
#include <cmath>
#include <algorithm>
#include <limits>
#include <random>

namespace rvc {

namespace {

constexpr size_t kHop16k = 160;
constexpr size_t kWindow16k = 32000;
constexpr size_t kPad16k = 16000;
constexpr size_t kGeneratorFrames = 200;
constexpr size_t kRealtimePitchCacheFrames = 1024;

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
            // NumPy's rint (used by the Golden path) rounds exact ties to even.
            pitch[t] = static_cast<int64_t>(std::clamp(std::nearbyint(coarse), 1.0f, 255.0f));
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
{
    reset_realtime();
}

void RVCInferencer::reset_realtime() {
    realtime_pitch_cache_.assign(kRealtimePitchCacheFrames, 0);
    realtime_pitchf_cache_.assign(kRealtimePitchCacheFrames, 0.0f);
    realtime_random_.seed(114514);
}

std::vector<float> RVCInferencer::infer(const std::vector<float>& audio) {
    if (!model_->loaded()) {
        throw std::runtime_error("RVC model not loaded");
    }
    if (!model_->has_generator()) {
        throw std::runtime_error("Offline Generator engine not loaded");
    }

    const auto profile_start = profile::Clock::now();
    std::vector<float> audio_16k;
    if (input_sample_rate_ != 16000) {
        audio_16k = resample(audio, input_sample_rate_, 16000);
    } else {
        audio_16k = audio;
    }
    peak_normalize(audio_16k, 0.95f);
    audio_16k = highpass_48hz(audio_16k);
    const auto profile_preprocessed = profile::Clock::now();

    const uint32_t model_sr = model_->config().sample_rate;
    const size_t hop_out = (kWindow16k / 2) * model_sr / 16000;
    const size_t window_out = kWindow16k * model_sr / 16000;
    const size_t pad_out = kPad16k * model_sr / 16000;

    size_t generator_frames = kGeneratorFrames;
    const auto generator_shape = model_->generator_engine().input_shape("feats");
    const bool dynamic_generator = generator_shape.size() >= 2
        && generator_shape[1] <= 0;
    if (generator_shape.size() >= 2 && generator_shape[1] > 0) {
        generator_frames = static_cast<size_t>(generator_shape[1]);
    }

    const auto padded = reflect_pad(audio_16k, kPad16k);
    const auto profile_padded = profile::Clock::now();
    const size_t full_frames = padded.size() / kHop16k;
    const auto f0_full = feature_extractor_->extract_f0(padded, 16000, f0_method_);
    const auto profile_f0 = profile::Clock::now();
    auto [pitch_full, pitchf_full] = f0_to_pitch(f0_full, pitch_shift_,
        std::max(f0_full.size(), padded.size() / kHop16k + 1));
    const auto profile_pitch = profile::Clock::now();

    // The correctness-first dynamic contract follows the original RVC path:
    // one reflect-padded prefix enters HuBERT and one Generator invocation.
    // Static engines retain the legacy fixed-window behavior below.
    if (dynamic_generator) {
        auto converted = infer_window(padded, pitchf_full, pitch_full);
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
        if (profile::enabled()) {
            const auto profile_done = profile::Clock::now();
            spdlog::info(
                "[profile][infer] input={} padded={} preprocess={:.3f}ms pad={:.3f}ms "
                "f0={:.3f}ms pitch={:.3f}ms hubert_generator_post={:.3f}ms total={:.3f}ms",
                audio.size(), padded.size(),
                profile::elapsed_ms(profile_start, profile_preprocessed),
                profile::elapsed_ms(profile_preprocessed, profile_padded),
                profile::elapsed_ms(profile_padded, profile_f0),
                profile::elapsed_ms(profile_f0, profile_pitch),
                profile::elapsed_ms(profile_pitch, profile_done),
                profile::elapsed_ms(profile_start, profile_done));
        }
        return converted;
    }

    // A statically exported full-length Generator can follow the Golden
    // pipeline directly. This avoids changing the model's sequence semantics
    // through 2-second chunking when a matching fixed-length export exists.
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
        // Golden-aligned: feed the reflect-padded window to HuBERT (matching
        // RVC pipeline.vc which pads by t_pad before HuBERT) and run the
        // generator at the engine's native T (398 for the streaming contract).
        // Crop the generator output by t_pad_tgt on each side like RVC's
        // [t_pad_tgt:-t_pad_tgt], then pad to the block contract so the
        // streaming pipeline's out_block_/emit_out_ assumptions hold.
        const size_t T = generator_frames;
        auto pitchf = slice_or_pad(pitchf_full, 0, T);
        std::vector<int64_t> pitch(T, 1);
        for (size_t t = 0; t < T; ++t) {
            const size_t source = std::min(t, pitch_full.size() - 1);
            pitch[t] = pitch_full[source];
        }
        auto converted = infer_window(padded, pitchf, pitch);
        const size_t pad_tgt = kPad16k * model_sr / 16000;
        if (converted.size() > 2 * pad_tgt) {
            converted = std::vector<float>(
                converted.begin() + static_cast<std::ptrdiff_t>(pad_tgt),
                converted.end() - static_cast<std::ptrdiff_t>(pad_tgt));
        }
        const size_t contract = kWindow16k * model_sr / 16000;
        if (converted.size() < contract) {
            converted.resize(contract, 0.0f);
        }
        converted = apply_rms_mix(audio_16k, std::move(converted));
        if (model_sr != output_sample_rate_) {
            converted = resample(converted, model_sr, output_sample_rate_);
        }
        limit_peak(converted, 0.99f);
        return converted;
    }

    const size_t hop_in = kWindow16k / 2;
    const size_t pad_tgt = kPad16k * model_sr / 16000;
    std::vector<float> assembled;
    assembled.reserve((padded.size() * model_sr / 16000) + window_out);
    size_t windows = 0;
    for (size_t start = 0; start < padded.size(); start += hop_in) {
        auto window = slice_or_pad(padded, start, kWindow16k);
        // Golden-aligned per window: reflect-pad the 2 s window to 4 s before
        // HuBERT (matching RVC pipeline.vc) and run at the engine's native T.
        auto hubert_input = reflect_pad(window, kPad16k);
        const size_t f0_start = start / kHop16k;
        std::vector<float> pitchf(generator_frames, 0.0f);
        std::vector<int64_t> pitch(generator_frames, 1);
        for (size_t t = 0; t < generator_frames; ++t) {
            const size_t src = std::min(f0_start + t, pitchf_full.size() - 1);
            pitchf[t] = pitchf_full[src];
            pitch[t] = pitch_full[src];
        }
        auto converted = infer_window(hubert_input, pitchf, pitch);
        if (converted.size() > 2 * pad_tgt) {
            converted = std::vector<float>(
                converted.begin() + static_cast<std::ptrdiff_t>(pad_tgt),
                converted.end() - static_cast<std::ptrdiff_t>(pad_tgt));
        }
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

std::vector<float> RVCInferencer::infer_realtime(
    const std::vector<float>& audio,
    const RvcRealtimeRequest& request
) {
    if (!model_->loaded() || !model_->has_realtime_generator()) {
        throw std::runtime_error("Realtime Generator engines not loaded");
    }
    if (input_sample_rate_ != 16000
        || output_sample_rate_ != model_->config().sample_rate
        || request.block_samples_16k == 0
        || request.block_samples_16k % kHop16k != 0
        || request.return_frames == 0
        || audio.size() % kHop16k != 0) {
        throw std::invalid_argument("Unsupported realtime RVC contract");
    }

    const auto profile_start = profile::Clock::now();
    const size_t frames = audio.size() / kHop16k;
    if (request.skip_head_frames + request.return_frames != frames) {
        throw std::invalid_argument("Realtime RVC skip/return frames do not cover input");
    }

    auto& front = model_->realtime_front_engine();
    auto& decoder = model_->realtime_decoder_engine();
    const auto front_feats_shape = front.input_shape("feats");
    if (front_feats_shape.size() != 3 || front_feats_shape[0] != 1
        || front_feats_shape[1] != static_cast<int64_t>(frames)
        || front_feats_shape[2] != static_cast<int64_t>(model_->config().emb_channels)) {
        throw std::invalid_argument("Realtime Generator front shape does not match request");
    }

    size_t f0_samples = request.block_samples_16k + 800;
    if (f0_method_ == "rmvpe") {
        f0_samples = 5120 * ((f0_samples - 1) / 5120 + 1) - kHop16k;
    }
    if (f0_samples > audio.size()) {
        throw std::invalid_argument("Realtime F0 window exceeds analysis input");
    }
    const std::vector<float> f0_audio(
        audio.end() - static_cast<std::ptrdiff_t>(f0_samples), audio.end()
    );
    const auto f0 = feature_extractor_->extract_f0_realtime(
        f0_audio, 16000, f0_method_);
    if (f0.size() < 5) {
        throw std::runtime_error("Realtime F0 extractor returned too few frames");
    }
    auto [new_pitch, new_pitchf] = f0_to_pitch(
        f0, pitch_shift_, f0.size()
    );

    const size_t shift = request.block_samples_16k / kHop16k;
    if (shift >= realtime_pitch_cache_.size()) {
        std::fill(realtime_pitch_cache_.begin(), realtime_pitch_cache_.end(), 0);
        std::fill(realtime_pitchf_cache_.begin(), realtime_pitchf_cache_.end(), 0.0f);
    } else {
        std::move(
            realtime_pitch_cache_.begin() + static_cast<std::ptrdiff_t>(shift),
            realtime_pitch_cache_.end(), realtime_pitch_cache_.begin()
        );
        std::fill(
            realtime_pitch_cache_.end() - static_cast<std::ptrdiff_t>(shift),
            realtime_pitch_cache_.end(), 0
        );
        std::move(
            realtime_pitchf_cache_.begin() + static_cast<std::ptrdiff_t>(shift),
            realtime_pitchf_cache_.end(), realtime_pitchf_cache_.begin()
        );
        std::fill(
            realtime_pitchf_cache_.end() - static_cast<std::ptrdiff_t>(shift),
            realtime_pitchf_cache_.end(), 0.0f
        );
    }

    // Upstream recomputes four overlap frames and drops three left-edge plus
    // one right-edge RMVPE frames before updating its persistent pitch cache.
    const size_t fresh_count = std::min(
        new_pitch.size() - 4, realtime_pitch_cache_.size()
    );
    const size_t fresh_source = new_pitch.size() - fresh_count - 1;
    const size_t fresh_target = realtime_pitch_cache_.size() - fresh_count;
    std::copy_n(
        new_pitch.begin() + static_cast<std::ptrdiff_t>(fresh_source),
        fresh_count,
        realtime_pitch_cache_.begin() + static_cast<std::ptrdiff_t>(fresh_target)
    );
    std::copy_n(
        new_pitchf.begin() + static_cast<std::ptrdiff_t>(fresh_source),
        fresh_count,
        realtime_pitchf_cache_.begin() + static_cast<std::ptrdiff_t>(fresh_target)
    );
    if (frames > realtime_pitch_cache_.size()) {
        throw std::invalid_argument("Realtime pitch cache is smaller than analysis input");
    }
    std::vector<int64_t> pitch(
        realtime_pitch_cache_.end() - static_cast<std::ptrdiff_t>(frames),
        realtime_pitch_cache_.end()
    );
    std::vector<float> pitchf(
        realtime_pitchf_cache_.end() - static_cast<std::ptrdiff_t>(frames),
        realtime_pitchf_cache_.end()
    );
    const auto profile_f0 = profile::Clock::now();

    auto feats = feature_extractor_->extract_features_realtime(audio, 16000);
    const uint32_t emb_dim = model_->config().emb_channels;
    if (feats.empty() || feats.size() % emb_dim != 0) {
        throw std::runtime_error("Realtime HuBERT feature shape is invalid");
    }
    const size_t feature_frames = feats.size() / emb_dim;
    if (model_->index().loaded() && index_rate_ > 0.0f) {
        const size_t index_start = std::min(
            request.skip_head_frames / 2, feature_frames
        );
        std::vector<float> tail(
            feats.begin() + static_cast<std::ptrdiff_t>(index_start * emb_dim),
            feats.end()
        );
        if (!tail.empty()) {
            auto indexed = model_->index().search(tail, emb_dim, index_rate_);
            std::copy(
                indexed.begin(), indexed.end(),
                feats.begin() + static_cast<std::ptrdiff_t>(index_start * emb_dim)
            );
        }
    }
    std::vector<float> resized_feats(frames * emb_dim, 0.0f);
    for (size_t target = 0; target < frames; ++target) {
        const size_t source = std::min(target / 2, feature_frames - 1);
        std::copy_n(
            feats.begin() + static_cast<std::ptrdiff_t>(source * emb_dim),
            emb_dim,
            resized_feats.begin() + static_cast<std::ptrdiff_t>(target * emb_dim)
        );
    }
    const auto profile_hubert = profile::Clock::now();

    const auto latent_shape = front.input_shape("latent_noise");
    if (latent_shape.size() != 3 || latent_shape[0] != 1
        || latent_shape[1] <= 0 || latent_shape[2] <= 0) {
        throw std::runtime_error("Realtime front latent-noise shape is invalid");
    }
    const size_t latent_channels = static_cast<size_t>(latent_shape[1]);
    const size_t latent_frames = static_cast<size_t>(latent_shape[2]);
    std::normal_distribution<float> normal(0.0f, 1.0f);
    std::uniform_real_distribution<float> uniform(0.0f, 1.0f);
    std::vector<float> latent_noise(latent_channels * latent_frames);
    for (float& value : latent_noise) value = normal(realtime_random_);

    std::vector<int64_t> sid_int64 = {model_->config().spk_id};
    std::vector<int64_t> p_len_int64 = {static_cast<int64_t>(frames)};
    std::vector<float> sid_float = {static_cast<float>(model_->config().spk_id)};
    std::vector<float> p_len_float = {static_cast<float>(frames)};
    std::vector<float> pitch_float(pitch.begin(), pitch.end());
    const auto typed_input = [](
        IEngine& engine, const char* name, std::vector<int64_t> shape,
        std::vector<float> floats, std::vector<int64_t> int64s
    ) {
        const auto type = engine.input_type(name).value_or(OnnxInput::Type::Float);
        return OnnxInput{
            name, std::move(shape), type, std::move(floats), std::move(int64s)
        };
    };
    std::vector<OnnxInput> front_inputs;
    front_inputs.reserve(5);
    front_inputs.push_back(typed_input(
        front, "feats", {1, static_cast<int64_t>(frames), static_cast<int64_t>(emb_dim)},
        std::move(resized_feats), {}
    ));
    front_inputs.push_back(typed_input(
        front, "p_len", {1}, std::move(p_len_float), std::move(p_len_int64)
    ));
    front_inputs.push_back(typed_input(
        front, "pitch", {1, static_cast<int64_t>(frames)},
        std::move(pitch_float), std::move(pitch)
    ));
    front_inputs.push_back(typed_input(
        front, "sid", {1}, sid_float, sid_int64
    ));
    front_inputs.push_back(typed_input(
        front, "latent_noise",
        {1, static_cast<int64_t>(latent_channels), static_cast<int64_t>(latent_frames)},
        std::move(latent_noise), {}
    ));
    auto z = front.run(front_inputs, {"z"});
    if (z.size() != latent_channels * request.return_frames) {
        throw std::runtime_error("Realtime front output shape is invalid");
    }
    const auto profile_front = profile::Clock::now();

    std::vector<float> decoder_pitchf(request.return_frames, 0.0f);
    std::copy_n(
        pitchf.begin() + static_cast<std::ptrdiff_t>(request.skip_head_frames),
        request.return_frames, decoder_pitchf.begin()
    );
    const auto source_shape = decoder.input_shape("source_noise");
    const size_t source_dim = source_shape.size() == 3 && source_shape[2] > 0
        ? static_cast<size_t>(source_shape[2]) : 1;
    const size_t output_per_frame = model_->config().sample_rate / 100;
    std::vector<float> source_phase(source_dim);
    for (float& value : source_phase) value = uniform(realtime_random_);
    source_phase.front() = 0.0f;
    std::vector<float> source_noise(
        request.return_frames * output_per_frame * source_dim
    );
    for (float& value : source_noise) value = normal(realtime_random_);

    std::vector<OnnxInput> decoder_inputs;
    decoder_inputs.reserve(5);
    decoder_inputs.push_back(typed_input(
        decoder, "z",
        {1, static_cast<int64_t>(latent_channels), static_cast<int64_t>(request.return_frames)},
        std::move(z), {}
    ));
    decoder_inputs.push_back(typed_input(
        decoder, "pitchf", {1, static_cast<int64_t>(request.return_frames)},
        std::move(decoder_pitchf), {}
    ));
    decoder_inputs.push_back(typed_input(
        decoder, "sid", {1}, std::move(sid_float), std::move(sid_int64)
    ));
    decoder_inputs.push_back(typed_input(
        decoder, "source_phase", {1, 1, static_cast<int64_t>(source_dim)},
        std::move(source_phase), {}
    ));
    decoder_inputs.push_back(typed_input(
        decoder, "source_noise",
        {1, static_cast<int64_t>(request.return_frames * output_per_frame),
         static_cast<int64_t>(source_dim)},
        std::move(source_noise), {}
    ));
    auto output = decoder.run(decoder_inputs, {"audio"});
    if (output.size() != request.output_samples) {
        throw std::runtime_error("Realtime decoder output shape is invalid");
    }
    if (profile::enabled()) {
        const auto profile_done = profile::Clock::now();
        spdlog::info(
            "[profile][realtime] input={} frames={} f0={:.3f}ms hubert={:.3f}ms "
            "front={:.3f}ms decoder={:.3f}ms total={:.3f}ms",
            audio.size(), frames,
            profile::elapsed_ms(profile_start, profile_f0),
            profile::elapsed_ms(profile_f0, profile_hubert),
            profile::elapsed_ms(profile_hubert, profile_front),
            profile::elapsed_ms(profile_front, profile_done),
            profile::elapsed_ms(profile_start, profile_done)
        );
    }
    return output;
}

std::vector<float> RVCInferencer::infer_window(
    const std::vector<float>& audio_16k,
    const std::vector<float>& pitchf,
    const std::vector<int64_t>& pitch
) {
    const auto profile_start = profile::Clock::now();
    auto feats = feature_extractor_->extract_features(audio_16k, 16000);
    const auto profile_hubert = profile::Clock::now();
    const auto unindexed = feats;
    feats = apply_index(feats);
    const auto profile_index = profile::Clock::now();
    auto output = synthesize_window(feats, pitchf, pitch, unindexed);
    if (profile::enabled()) {
        const auto profile_done = profile::Clock::now();
        spdlog::info(
            "[profile][window] input={} features={} hubert={:.3f}ms feature_copies_index={:.3f}ms "
            "generator={:.3f}ms total={:.3f}ms",
            audio_16k.size(), feats.size(),
            profile::elapsed_ms(profile_start, profile_hubert),
            profile::elapsed_ms(profile_hubert, profile_index),
            profile::elapsed_ms(profile_index, profile_done),
            profile::elapsed_ms(profile_start, profile_done));
    }
    return output;
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

    const auto profile_start = profile::Clock::now();
    uint32_t emb_dim = model_->config().emb_channels;
    size_t T = kGeneratorFrames;
    const auto generator_shape = model_->generator_engine().input_shape("feats");
    if (generator_shape.size() >= 2 && generator_shape[1] > 0) {
        T = static_cast<size_t>(generator_shape[1]);
    } else if (generator_shape.size() >= 2 && generator_shape[1] <= 0) {
        T = 2 * (feats.size() / emb_dim);
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
                                  std::vector<float>&& floats, std::vector<int64_t>&& int64s) {
        const auto type = model_->generator_engine().input_type(name).value_or(OnnxInput::Type::Float);
        return OnnxInput{name, shape, type, std::move(floats), std::move(int64s)};
    };
    std::vector<OnnxInput> inputs;
    inputs.reserve(8);
    inputs.push_back(typed_input(
        "feats", feats_shape, std::move(feats_reshaped), {}
    ));
    inputs.push_back(typed_input(
        "p_len", p_len_shape, std::move(p_len_float), std::move(p_len_int64)
    ));
    inputs.push_back(typed_input(
        "pitch", pitch_shape, std::move(pitch_float), std::move(pitch_fixed)
    ));
    inputs.push_back(typed_input(
        "pitchf", pitchf_shape, std::move(pitchf_fixed), {}
    ));
    inputs.push_back(typed_input(
        "sid", sid_shape, std::move(sid_float), std::move(sid_int64)
    ));

    if (model_->generator_engine().input_type("latent_noise")) {
        const auto latent_model_shape =
            model_->generator_engine().input_shape("latent_noise");
        const size_t channels = latent_model_shape.size() >= 2
            && latent_model_shape[1] > 0
            ? static_cast<size_t>(latent_model_shape[1]) : 192;
        const auto source_model_shape =
            model_->generator_engine().input_shape("source_noise");
        const size_t source_dim = source_model_shape.size() >= 3
            && source_model_shape[2] > 0
            ? static_cast<size_t>(source_model_shape[2]) : 1;
        const size_t output_per_frame = model_->config().sample_rate / 100;

        // Resetting per prefix preserves the Golden full-history contract:
        // overlapping latent positions receive the same sampled values.
        std::mt19937 random(114514);
        std::normal_distribution<float> normal(0.0f, 1.0f);
        std::uniform_real_distribution<float> uniform(0.0f, 1.0f);
        std::vector<float> latent_noise(channels * T);
        for (float& value : latent_noise) value = normal(random);
        std::vector<float> source_phase(source_dim);
        for (float& value : source_phase) value = uniform(random);
        source_phase.front() = 0.0f;
        std::vector<float> source_noise(T * output_per_frame * source_dim);
        for (float& value : source_noise) value = normal(random);

        inputs.push_back(typed_input(
            "latent_noise",
            {1, static_cast<int64_t>(channels), static_cast<int64_t>(T)},
            std::move(latent_noise), {}
        ));
        inputs.push_back(typed_input(
            "source_phase", {1, 1, static_cast<int64_t>(source_dim)},
            std::move(source_phase), {}
        ));
        inputs.push_back(typed_input(
            "source_noise",
            {1, static_cast<int64_t>(T * output_per_frame),
             static_cast<int64_t>(source_dim)},
            std::move(source_noise), {}
        ));
    }
    const auto profile_prepared = profile::Clock::now();
    auto output = model_->generator_engine().run(inputs, {"audio"});
    if (profile::enabled()) {
        const auto profile_done = profile::Clock::now();
        spdlog::info(
            "[profile][generator] frames={} prepare={:.3f}ms engine={:.3f}ms total={:.3f}ms",
            T, profile::elapsed_ms(profile_start, profile_prepared),
            profile::elapsed_ms(profile_prepared, profile_done),
            profile::elapsed_ms(profile_start, profile_done));
    }
    return output;
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
