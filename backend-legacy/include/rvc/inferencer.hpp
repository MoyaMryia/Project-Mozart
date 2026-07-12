#pragma once

#include <vector>
#include <memory>
#include <cstdint>

#include "rvc/model_loader.hpp"
#include "rvc/feature_extractor.hpp"

namespace rvc {

// ──────────────────────────────────────────────────────────
// RVC Inferencer: wraps generator inference for a single frame
// Input:  float32 mono @ input_sample_rate (e.g. 16 kHz from contract)
// Output: float32 mono @ output_sample_rate (e.g. 48 kHz from generator)
// ──────────────────────────────────────────────────────────
class RVCInferencer {
public:
    RVCInferencer(
        std::shared_ptr<RVCModel> model,
        std::shared_ptr<FeatureExtractor> feature_extractor,
        uint32_t input_sample_rate = 16000,
        uint32_t output_sample_rate = 48000,
        int pitch_shift = 0,
        float index_rate = 0.75f,
        int filter_radius = 3,
        float rms_mix_rate = 0.25f,
        float protect = 0.33f
    );

    // Run full RVC inference on a float32 mono audio chunk
    std::vector<float> infer(const std::vector<float>& audio);

private:
    std::shared_ptr<RVCModel> model_;
    std::shared_ptr<FeatureExtractor> feature_extractor_;
    uint32_t input_sample_rate_;
    uint32_t output_sample_rate_;
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

    std::vector<float> run_generator(
        const std::vector<float>& feats,
        const std::vector<float>& f0,
        const std::vector<float>& original_audio
    );
};

} // namespace rvc
