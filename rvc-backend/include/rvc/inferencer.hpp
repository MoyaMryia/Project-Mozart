#pragma once

#include <vector>
#include <memory>
#include <cstdint>

#include "rvc/model_loader.hpp"
#include "rvc/feature_extractor.hpp"

namespace rvc {

class RVCInferencer {
public:
    RVCInferencer(
        std::shared_ptr<RVCModel> model,
        std::shared_ptr<FeatureExtractor> feature_extractor,
        uint32_t input_sample_rate = 16000,
        uint32_t output_sample_rate = 48000,
        std::string f0_method = "rmvpe",
        int pitch_shift = 0,
        float index_rate = 0.0f,
        int filter_radius = 3,
        float rms_mix_rate = 1.0f,
        float protect = 0.33f
    );

    std::vector<float> infer(const std::vector<float>& audio);

private:
    std::shared_ptr<RVCModel> model_;
    std::shared_ptr<FeatureExtractor> feature_extractor_;
    uint32_t input_sample_rate_;
    uint32_t output_sample_rate_;
    std::string f0_method_;
    int pitch_shift_;
    float index_rate_;
    int filter_radius_;
    float rms_mix_rate_;
    float protect_;

    std::vector<float> resample(
        const std::vector<float>& audio,
        uint32_t src_sr, uint32_t dst_sr
    );

    std::vector<float> apply_index(const std::vector<float>& feats);

    std::vector<float> synthesize_window(
        const std::vector<float>& feats,
        const std::vector<float>& pitchf,
        const std::vector<int64_t>& pitch,
        const std::vector<float>& unindexed_feats
    );

    std::vector<float> apply_rms_mix(
        const std::vector<float>& original_16k,
        std::vector<float> audio_out
    );

    std::vector<float> infer_window(
        const std::vector<float>& audio_16k,
        const std::vector<float>& pitchf,
        const std::vector<int64_t>& pitch
    );
};

} // namespace rvc
