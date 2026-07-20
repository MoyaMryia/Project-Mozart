#include "rvc/inferencer.hpp"
#include <spdlog/spdlog.h>
#include <cmath>
#include <cstring>

#ifdef USE_LIBTORCH
#include <torch/cuda.h>
#endif

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
    float protect,
    const std::string& de_narrator_path
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
    , de_narrator_path_(de_narrator_path)
{
#ifdef USE_LIBTORCH
    if (!de_narrator_path_.empty()) {
        try {
            de_narrator_ = torch::jit::load(de_narrator_path_);
            de_narrator_.eval();
            if (torch::cuda::is_available()) {
                de_narrator_.to(torch::kCUDA);
            }
            de_narrator_loaded_ = true;
            spdlog::info("De-narrator TorchScript model loaded from {}", de_narrator_path_);
        } catch (const std::exception& e) {
            spdlog::error("Failed to load de-narrator model: {}", e.what());
            de_narrator_loaded_ = false;
        }
    }
#endif
}

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

void RVCInferencer::f0_to_pitch(
    const std::vector<float>& f0,
    std::vector<int64_t>& pitch,
    std::vector<float>& pitchf
) {
    constexpr int num_bins = 256;
    constexpr float f0_min = 10.0f;
    constexpr float f0_max = 2000.0f;
    constexpr float log_ratio = 1.0f / std::log(f0_max / f0_min);

    size_t T = f0.size();
    pitch.resize(T);
    pitchf.resize(T);

    for (size_t i = 0; i < T; ++i) {
        float f = f0[i];
        if (f <= f0_min || f >= f0_max || std::isnan(f)) {
            pitch[i] = 0;
            pitchf[i] = 0.0f;
        } else {
            float bin = std::log(f / f0_min) * log_ratio * static_cast<float>(num_bins - 1);
            int64_t bin_idx = static_cast<int64_t>(std::floor(bin));
            bin_idx = std::max<int64_t>(0, std::min<int64_t>(bin_idx, num_bins - 1));
            pitch[i] = bin_idx;
            pitchf[i] = bin - static_cast<float>(bin_idx);
        }
    }
}

std::vector<float> RVCInferencer::run_generator(
    const std::vector<float>& feats,
    const std::vector<float>& f0,
    const std::vector<float>& original_audio
) {
#ifdef USE_LIBTORCH
    if (!de_narrator_loaded_) {
        throw std::runtime_error("De-narrator model not loaded");
    }

    size_t T = f0.size();
    if (T == 0) {
        spdlog::warn("run_generator: zero-length f0, returning silence");
        return std::vector<float>(original_audio.size(), 0.0f);
    }

    size_t emb_channels = model_->config().emb_channels;
    size_t feats_expected = T * emb_channels;
    if (feats.size() != feats_expected) {
        spdlog::warn("run_generator: feats size {} != T*emb_channels = {}*{} = {}",
                     feats.size(), T, emb_channels, feats_expected);
    }

    torch::Tensor feats_tensor = torch::from_blob(
        const_cast<float*>(feats.data()),
        {static_cast<int64_t>(1), static_cast<int64_t>(T), static_cast<int64_t>(emb_channels)},
        torch::kFloat32
    ).clone();

    torch::Tensor p_len = torch::tensor({static_cast<int64_t>(T)}, torch::kInt64);

    std::vector<int64_t> pitch_vec;
    std::vector<float> pitchf_vec;
    f0_to_pitch(f0, pitch_vec, pitchf_vec);

    torch::Tensor pitch = torch::from_blob(
        pitch_vec.data(),
        {static_cast<int64_t>(1), static_cast<int64_t>(T)},
        torch::kInt64
    ).clone();

    torch::Tensor pitchf = torch::from_blob(
        pitchf_vec.data(),
        {static_cast<int64_t>(1), static_cast<int64_t>(T)},
        torch::kFloat32
    ).clone();

    int64_t spk_id = static_cast<int64_t>(model_->config().spk_id);
    torch::Tensor sid = torch::tensor({spk_id}, torch::kInt64);

    if (torch::cuda::is_available()) {
        feats_tensor = feats_tensor.to(torch::kCUDA);
        pitch = pitch.to(torch::kCUDA);
        pitchf = pitchf.to(torch::kCUDA);
        sid = sid.to(torch::kCUDA);
    }

    std::vector<torch::jit::IValue> inputs;
    inputs.push_back(feats_tensor);
    inputs.push_back(p_len);
    inputs.push_back(pitch);
    inputs.push_back(pitchf);
    inputs.push_back(sid);

    torch::DeviceType current_device = torch::cuda::is_available()
        ? torch::kCUDA : torch::kCPU;
    de_narrator_.to(current_device);

    torch::NoGradGuard no_grad;
    auto output = de_narrator_.forward(inputs);
    auto audio_tensor = output.toTuple()->elements()[0].toTensor();

    audio_tensor = audio_tensor.to(torch::kCPU);

    int64_t audio_len = audio_tensor.numel();
    std::vector<float> result(audio_len);
    std::memcpy(result.data(), audio_tensor.data_ptr<float>(),
                static_cast<size_t>(audio_len) * sizeof(float));

    return result;
#else
    spdlog::warn("Real generator inference not implemented; upsampling input to {}Hz",
                 output_sample_rate_);

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
#endif
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
    spdlog::debug("Index search not implemented in Phase 1; returning original features");
    return feats;
}

} // namespace rvc
