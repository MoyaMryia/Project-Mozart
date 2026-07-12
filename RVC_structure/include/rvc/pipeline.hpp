#pragma once

#include <vector>
#include <memory>
#include <cstdint>
#include <filesystem>
#include <optional>

#include "rvc/model_loader.hpp"
#include "rvc/feature_extractor.hpp"
#include "rvc/inferencer.hpp"

namespace rvc {

// ──────────────────────────────────────────────────────────
// Abstract RVC pipeline
// ──────────────────────────────────────────────────────────
class RVCPipelineBase {
public:
    virtual ~RVCPipelineBase() = default;

    // Input: float32 mono audio at input_sample_rate
    // Output: float32 mono audio at output_sample_rate
    virtual std::vector<float> process(const std::vector<float>& audio) = 0;
};

// ──────────────────────────────────────────────────────────
// Mock pipeline: pass-through with simple upsampling
// ──────────────────────────────────────────────────────────
class MockRVCPipeline : public RVCPipelineBase {
public:
    MockRVCPipeline(
        uint32_t input_sample_rate = 16000,
        uint32_t output_sample_rate = 48000
    );

    std::vector<float> process(const std::vector<float>& audio) override;

private:
    uint32_t input_sample_rate_;
    uint32_t output_sample_rate_;

    std::vector<float> upsample_linear(const std::vector<float>& audio, float ratio);
};

// ──────────────────────────────────────────────────────────
// Real RVC pipeline: full inference
// ──────────────────────────────────────────────────────────
class RealRVCPipeline : public RVCPipelineBase {
public:
    RealRVCPipeline(
        std::shared_ptr<ModelManager> model_manager,
        const std::filesystem::path& hubert_path,
        const std::optional<std::filesystem::path>& rmvpe_path,
        uint32_t input_sample_rate = 16000,
        uint32_t output_sample_rate = 48000,
        const std::string& device = "cuda",
        bool half = false
    );

    std::vector<float> process(const std::vector<float>& audio) override;
    bool switch_model(const std::string& model_id);

    std::shared_ptr<ModelManager> model_manager() const { return model_manager_; }

private:
    std::shared_ptr<ModelManager> model_manager_;
    std::shared_ptr<FeatureExtractor> feature_extractor_;
    std::shared_ptr<RVCInferencer> inferencer_;

    uint32_t input_sample_rate_;
    uint32_t output_sample_rate_;
    std::string device_;
    bool half_;

    void rebuild_inferencer();
};

// ──────────────────────────────────────────────────────────
// Factory
// ──────────────────────────────────────────────────────────
class RVCPipelineFactory {
public:
    static std::unique_ptr<RVCPipelineBase> create(
        bool mock_mode,
        const std::filesystem::path& models_dir,
        const std::filesystem::path& hubert_path,
        const std::optional<std::filesystem::path>& rmvpe_path,
        uint32_t input_sample_rate = 16000,
        uint32_t output_sample_rate = 48000,
        const std::string& device = "cuda",
        bool half = false
    );
};

} // namespace rvc
