#pragma once
#include <mozart/core/frame_meta.hpp>
#include <memory>
#include <string>

namespace mozart {

// RVC inference parameters (complete set from UDP-Contract version)
struct RVCParams {
    int pitch_shift = 0;        // Semitones to shift pitch
    float index_rate = 0.0f;    // Feature index retrieval rate (0=disable)
    int filter_radius = 3;      // Median filter radius for F0
    float rms_mix_rate = 0.25f; // Input/output volume mix ratio
    float protect = 0.33f;      // Protect voiceless consonants (0-0.5)
    int frames_per_inference = 1; // Batch multiple frames together
};

// Abstract RVC pipeline
class RVCPipeline {
public:
    virtual ~RVCPipeline() = default;

    // Process input frame (16kHz) -> output frame (48kHz)
    virtual void process(const FrameBuf& in, OutputFrameBuf& out) = 0;

    // Reset streaming state (called on segment_id change)
    virtual void reset_state() = 0;

    // Get pipeline name
    virtual const char* name() const = 0;
};

// Mock pipeline: simple upsampling (for testing without models)
class MockRVCPipeline : public RVCPipeline {
public:
    void process(const FrameBuf& in, OutputFrameBuf& out) override;
    void reset_state() override {}
    const char* name() const override { return "mock"; }
};

// Real pipeline: HuBERT + RMVPE + Generator
class RealRVCPipeline : public RVCPipeline {
public:
    struct Config {
        std::string model_dir;
        std::string hubert_path;
        RVCParams params;
    };

    explicit RealRVCPipeline(const Config& cfg);
    ~RealRVCPipeline();

    void process(const FrameBuf& in, OutputFrameBuf& out) override;
    void reset_state() override;
    const char* name() const override { return "real"; }

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

// Factory: create pipeline based on config
std::unique_ptr<RVCPipeline> create_pipeline(
    bool mock_mode,
    const RealRVCPipeline::Config& cfg);

}  // namespace mozart
