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

struct RvcMockConfig {
    bool generator = false;
    bool hubert = false;
    bool rmvpe = false;
};

struct RvcParameters {
    std::string f0_method = "rmvpe";
    int pitch_shift = 0;
    float index_rate = 0.0f;
    int filter_radius = 3;
    float rms_mix_rate = 1.0f;
    float protect = 0.33f;
};

class RVCPipelineBase {
public:
    virtual ~RVCPipelineBase() = default;

    virtual std::vector<float> process(const std::vector<float>& audio) = 0;

    virtual std::vector<float> process_realtime(
        const std::vector<float>& audio, const RvcRealtimeRequest& request
    ) {
        auto output = process(audio);
        if (output.size() > request.output_samples) {
            output.erase(
                output.begin(),
                output.end() - static_cast<std::ptrdiff_t>(request.output_samples)
            );
        }
        return output;
    }
    virtual void reset_realtime() {}

    virtual bool switch_model(const std::string&) { return false; }
    virtual std::string current_model_id() const { return ""; }
    virtual bool is_mock() const { return true; }
    virtual std::map<std::string, std::string> model_info() const { return {}; }
    virtual RvcParameters parameters() const { return {}; }
    virtual bool set_parameters(const RvcParameters&) { return false; }
    virtual bool supports_quality_streaming() const { return false; }
    virtual bool supports_realtime_streaming() const { return false; }
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
        bool half = false,
        RvcMockConfig mock = {},
        RvcParameters parameters = {},
        const std::optional<std::filesystem::path>& realtime_hubert_path = std::nullopt,
        const std::optional<std::filesystem::path>& realtime_rmvpe_path = std::nullopt
    );

    std::vector<float> process(const std::vector<float>& audio) override;
    std::vector<float> process_realtime(
        const std::vector<float>& audio,
        const RvcRealtimeRequest& request
    ) override;
    void reset_realtime() override;
    bool switch_model(const std::string& model_id) override;
    std::string current_model_id() const override;
    bool is_mock() const override { return false; }
    std::map<std::string, std::string> model_info() const override;
    RvcParameters parameters() const override { return parameters_; }
    bool set_parameters(const RvcParameters& parameters) override;
    bool supports_quality_streaming() const override;
    bool supports_realtime_streaming() const override;

    std::shared_ptr<ModelManager> model_manager() const { return model_manager_; }

private:
    std::shared_ptr<ModelManager> model_manager_;
    std::shared_ptr<FeatureExtractor> feature_extractor_;
    std::shared_ptr<RVCInferencer> inferencer_;

    uint32_t input_sample_rate_;
    uint32_t output_sample_rate_;
    std::string device_;
    bool half_;
    RvcMockConfig mock_;
    RvcParameters parameters_;

    void rebuild_inferencer();
};

class RVCPipelineFactory {
public:
    static std::unique_ptr<RVCPipelineBase> create(
        RvcMockConfig mock,
        const std::filesystem::path& models_dir,
        const std::filesystem::path& hubert_path,
        const std::optional<std::filesystem::path>& rmvpe_path,
        uint32_t input_sample_rate = 16000,
        uint32_t output_sample_rate = 48000,
        const std::string& device = "cuda",
        bool half = false,
        RvcParameters parameters = {},
        const std::optional<std::filesystem::path>& realtime_hubert_path = std::nullopt,
        const std::optional<std::filesystem::path>& realtime_rmvpe_path = std::nullopt
    );
};

} // namespace rvc
