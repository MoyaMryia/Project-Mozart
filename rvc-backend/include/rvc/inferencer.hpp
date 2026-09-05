#pragma once

#include <vector>
#include <memory>
#include <cstdint>
#include <cstddef>
#include <random>

#include "rvc/model_loader.hpp"
#include "rvc/feature_extractor.hpp"

namespace rvc {

struct RvcRealtimeRequest {
    size_t block_samples_16k = 0;
    size_t skip_head_frames = 0;
    size_t return_frames = 0;
    size_t output_samples = 0;
};

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
    std::vector<float> infer_realtime(
        const std::vector<float>& audio,
        const RvcRealtimeRequest& request
    );
    void reset_realtime();

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
    std::vector<int64_t> realtime_pitch_cache_;
    std::vector<float> realtime_pitchf_cache_;
    std::mt19937 realtime_random_{114514};

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
