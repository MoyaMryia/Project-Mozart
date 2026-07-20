#pragma once

#include <vector>
#include <memory>
#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>
#include <map>

#include "rvc/model_loader.hpp"
#include "rvc/feature_extractor.hpp"
#include "rvc/inferencer.hpp"

namespace rvc {

class RVCPipelineBase {
public:
    virtual ~RVCPipelineBase() = default;

    virtual std::vector<float> process(const std::vector<float>& audio) = 0;

    virtual bool switch_model(const std::string&) { return false; }
    virtual std::string current_model_id() const { return ""; }
    virtual bool is_mock() const { return true; }
    virtual std::map<std::string, std::string> model_info() const { return {}; }
};

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
    bool switch_model(const std::string& model_id) override;
    std::string current_model_id() const override;
    bool is_mock() const override { return false; }
    std::map<std::string, std::string> model_info() const override;

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
