// rvc/rvc_pipeline.hpp
// Orchestrates the RVC post-processing pipeline.
//
// Contract input : 16kHz / mono / float32 / 320 samples + FrameMeta
// Pipeline output: 48kHz / mono / float32 / 960 samples
//
// In mock_mode, the pipeline performs a simple linear upsample (16k->48k)
// without any neural inference, so the full IO chain can be exercised.
// In real mode, it runs HuBERT + RMVPE + Generator via ONNX Runtime.
#pragma once

#include "contract/contract_source.hpp"
#include "frame_meta.hpp"

#include <memory>
#include <span>
#include <string>

namespace mozart {

inline constexpr int kOutputRate = 48000;
inline constexpr int kOutputSamples = 960;  // 48kHz * 20ms

class RVCPipeline {
public:
    virtual ~RVCPipeline() = default;

    // Run one frame. Output must hold at least kOutputSamples floats.
    virtual void run(std::span<const float> pcm16k,
                     const FrameMeta& meta,
                     std::span<float> out48k) = 0;

    // Called when segment_id changes across consecutive frames so derived
    // classes can reset any streaming state (overlaps, hidden states).
    virtual void reset_state() {}

    virtual const char* name() const = 0;
};

// Build a pipeline from config. When mock_mode is true, returns MockPipeline.
// Otherwise attempts to construct a RealPipeline using the provided engine.
std::unique_ptr<RVCPipeline> make_pipeline(
    bool mock_mode,
    const std::string& assets_dir,
    const std::string& hubert_path,
    const std::string& rmvpe_path,
    const std::string& models_dir,
    const std::string& device,
    float index_rate);

} // namespace mozart