#include "rvc/pipeline.hpp"
#include <spdlog/spdlog.h>
#include <algorithm>

namespace rvc {

// ──────────────────────────────────────────────────────────
// MockRVCPipeline
// ──────────────────────────────────────────────────────────
MockRVCPipeline::MockRVCPipeline(
    uint32_t input_sample_rate,
    uint32_t output_sample_rate
) : input_sample_rate_(input_sample_rate), output_sample_rate_(output_sample_rate) {}

std::vector<float> MockRVCPipeline::process(const std::vector<float>& audio) {
    if (input_sample_rate_ == output_sample_rate_) {
        return audio;
    }

    float ratio = static_cast<float>(output_sample_rate_) / static_cast<float>(input_sample_rate_);

    if (ratio == 3.0f) { // Common case: 16k -> 48k
        std::vector<float> result;
        result.reserve(audio.size() * 3);
        for (float s : audio) {
            result.push_back(s);
            result.push_back(s);
            result.push_back(s);
        }
        return result;
    }

    return upsample_linear(audio, ratio);
}

std::vector<float> MockRVCPipeline::upsample_linear(
    const std::vector<float>& audio, float ratio
) {
    size_t n_out = static_cast<size_t>(static_cast<float>(audio.size()) * ratio);
    std::vector<float> result(n_out);
    for (size_t i = 0; i < n_out; ++i) {
        float src_idx = static_cast<float>(i) / ratio;
        size_t idx0 = static_cast<size_t>(src_idx);
        size_t idx1 = std::min(idx0 + 1, audio.size() - 1);
        float frac = src_idx - static_cast<float>(idx0);
        result[i] = audio[idx0] * (1.0f - frac) + audio[idx1] * frac;
    }
    return result;
}

// ──────────────────────────────────────────────────────────
// RealRVCPipeline
// ──────────────────────────────────────────────────────────
RealRVCPipeline::RealRVCPipeline(
    std::shared_ptr<ModelManager> model_manager,
    const std::filesystem::path& hubert_path,
    const std::optional<std::filesystem::path>& rmvpe_path,
    uint32_t input_sample_rate,
    uint32_t output_sample_rate,
    const std::string& device,
    bool half
)
    : model_manager_(model_manager)
    , input_sample_rate_(input_sample_rate)
    , output_sample_rate_(output_sample_rate)
    , device_(device)
    , half_(half)
{
    feature_extractor_ = std::make_shared<FeatureExtractor>(
        hubert_path, rmvpe_path, device, half
    );
    rebuild_inferencer();
}

void RealRVCPipeline::rebuild_inferencer() {
    auto model = model_manager_->current_model();
    if (!model || !model->loaded()) {
        inferencer_.reset();
        return;
    }
    inferencer_ = std::make_shared<RVCInferencer>(
        model, feature_extractor_,
        input_sample_rate_, output_sample_rate_
    );
    spdlog::info("RVC inferencer rebuilt for model '{}'", model->id());
}

bool RealRVCPipeline::switch_model(const std::string& model_id) {
    bool success = model_manager_->load_model(model_id);
    if (success) {
        rebuild_inferencer();
    }
    return success;
}

std::vector<float> RealRVCPipeline::process(const std::vector<float>& audio) {
    if (!inferencer_) {
        spdlog::warn("No RVC model loaded; returning mock upsampled input");
        MockRVCPipeline mock(input_sample_rate_, output_sample_rate_);
        return mock.process(audio);
    }

    try {
        return inferencer_->infer(audio);
    } catch (const std::exception& e) {
        spdlog::error("RVC inference failed: {}", e.what());
        MockRVCPipeline mock(input_sample_rate_, output_sample_rate_);
        return mock.process(audio);
    }
}

// ──────────────────────────────────────────────────────────
// Factory
// ──────────────────────────────────────────────────────────
std::unique_ptr<RVCPipelineBase> RVCPipelineFactory::create(
    bool mock_mode,
    const std::filesystem::path& models_dir,
    const std::filesystem::path& hubert_path,
    const std::optional<std::filesystem::path>& rmvpe_path,
    uint32_t input_sample_rate,
    uint32_t output_sample_rate,
    const std::string& device,
    bool half
) {
    if (mock_mode) {
        spdlog::info(
            "RVC pipeline initialized in MOCK mode ({}Hz -> {}Hz)",
            input_sample_rate, output_sample_rate
        );
        return std::make_unique<MockRVCPipeline>(
            input_sample_rate, output_sample_rate
        );
    }

    auto model_manager = std::make_shared<ModelManager>(models_dir, device, half);
    auto models = model_manager->list_models();
    for (const auto& m : models) {
        auto it = m.find("exists");
        if (it != m.end() && it->second == "true") {
            try {
                model_manager->load_model(m.at("id"));
                break;
            } catch (...) {}
        }
    }

    auto pipeline = std::make_unique<RealRVCPipeline>(
        model_manager, hubert_path, rmvpe_path,
        input_sample_rate, output_sample_rate,
        device, half
    );
    spdlog::info(
        "RVC pipeline initialized in REAL mode ({}Hz -> {}Hz)",
        input_sample_rate, output_sample_rate
    );
    return pipeline;
}

} // namespace rvc
