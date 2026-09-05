#include "rvc/pipeline.hpp"
#include <spdlog/spdlog.h>
#include <algorithm>

namespace rvc {

MockRVCPipeline::MockRVCPipeline(
    uint32_t input_sample_rate,
    uint32_t output_sample_rate
) : input_sample_rate_(input_sample_rate), output_sample_rate_(output_sample_rate) {}

std::vector<float> MockRVCPipeline::process(const std::vector<float>& audio) {
    if (input_sample_rate_ == output_sample_rate_) {
        return audio;
    }

    float ratio = static_cast<float>(output_sample_rate_) / static_cast<float>(input_sample_rate_);

    if (ratio == 3.0f) {
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

RealRVCPipeline::RealRVCPipeline(
    std::shared_ptr<ModelManager> model_manager,
    const std::filesystem::path& hubert_path,
    const std::optional<std::filesystem::path>& rmvpe_path,
    uint32_t input_sample_rate,
    uint32_t output_sample_rate,
    const std::string& device,
    bool half,
    RvcMockConfig mock,
    RvcParameters parameters,
    const std::optional<std::filesystem::path>& realtime_hubert_path,
    const std::optional<std::filesystem::path>& realtime_rmvpe_path
)
    : model_manager_(model_manager)
    , input_sample_rate_(input_sample_rate)
    , output_sample_rate_(output_sample_rate)
    , device_(device)
    , half_(half)
    , mock_(mock)
    , parameters_(std::move(parameters))
{
    feature_extractor_ = std::make_shared<FeatureExtractor>(
        hubert_path, rmvpe_path, device, half, mock_.hubert, mock_.rmvpe,
        realtime_hubert_path, realtime_rmvpe_path
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
        input_sample_rate_, output_sample_rate_,
        parameters_.f0_method, parameters_.pitch_shift, parameters_.index_rate,
        parameters_.filter_radius, parameters_.rms_mix_rate, parameters_.protect
    );
    spdlog::info("RVC inferencer rebuilt for model '{}'", model->id());
}

bool RealRVCPipeline::set_parameters(const RvcParameters& parameters) {
    if (parameters.f0_method != "rmvpe" && parameters.f0_method != "harvest" && parameters.f0_method != "pm") return false;
    if (parameters.pitch_shift < -12 || parameters.pitch_shift > 12) return false;
    if (parameters.index_rate < 0.0f || parameters.index_rate > 1.0f) return false;
    if (parameters.filter_radius < 0 || parameters.filter_radius > 7) return false;
    if (parameters.rms_mix_rate < 0.0f || parameters.rms_mix_rate > 1.0f) return false;
    if (parameters.protect < 0.0f || parameters.protect > 0.5f) return false;
    parameters_ = parameters;
    rebuild_inferencer();
    return true;
}

bool RealRVCPipeline::switch_model(const std::string& model_id) {
    bool success = model_manager_->load_model(model_id);
    if (success) {
        rebuild_inferencer();
    }
    return success;
}

std::string RealRVCPipeline::current_model_id() const {
    auto model = model_manager_->current_model();
    return model ? model->id() : "";
}

std::map<std::string, std::string> RealRVCPipeline::model_info() const {
    std::map<std::string, std::string> info;
    auto model = model_manager_->current_model();
    if (model) {
        info["id"] = model->id();
        info["loaded"] = model->loaded() ? "true" : "false";
        info["has_index"] = model->index().loaded() ? "true" : "false";
        info["has_generator"] = model->has_generator() ? "true" : "false";
        info["has_realtime_generator"] = model->has_realtime_generator()
            ? "true" : "false";
        info["sample_rate"] = std::to_string(model->config().sample_rate);
    } else {
        info["id"] = "";
        info["loaded"] = "false";
    }
    return info;
}

bool RealRVCPipeline::supports_quality_streaming() const {
    const auto model = model_manager_->current_model();
    if (!model || !model->loaded() || !model->has_generator()) return false;
    const auto shape = model->generator_engine().input_shape("feats");
    return shape.size() >= 2 && shape[1] <= 0
        && model->generator_engine().input_type("latent_noise").has_value();
}

bool RealRVCPipeline::supports_realtime_streaming() const {
    const auto model = model_manager_->current_model();
    if (!model || !model->loaded() || !model->has_realtime_generator()
        || model->config().sample_rate != output_sample_rate_
        || input_sample_rate_ != 16000
        || !feature_extractor_->supports_realtime_hubert_samples(44800)) {
        return false;
    }
    if (parameters_.f0_method == "rmvpe"
        && !feature_extractor_->supports_realtime_rmvpe_frames(32)) {
        return false;
    }
    const auto front_shape = model->realtime_front_engine().input_shape("feats");
    const auto decoder_shape = model->realtime_decoder_engine().input_shape("z");
    return front_shape.size() == 3 && front_shape[1] == 280
        && front_shape[2] == static_cast<int64_t>(model->config().emb_channels)
        && decoder_shape.size() == 3 && decoder_shape[2] == 30;
}

std::vector<float> RealRVCPipeline::process(const std::vector<float>& audio) {
    if (mock_.generator) {
        MockRVCPipeline mock(input_sample_rate_, output_sample_rate_);
        return mock.process(audio);
    }
    if (!inferencer_) {
        throw std::runtime_error("RVC generator is unavailable and rvc.mock.generator is false");
    }

    return inferencer_->infer(audio);
}

std::vector<float> RealRVCPipeline::process_realtime(
    const std::vector<float>& audio,
    const RvcRealtimeRequest& request
) {
    if (mock_.generator) {
        return RVCPipelineBase::process_realtime(audio, request);
    }
    if (!inferencer_) {
        throw std::runtime_error("RVC realtime Generator is unavailable");
    }
    return inferencer_->infer_realtime(audio, request);
}

void RealRVCPipeline::reset_realtime() {
    if (inferencer_) inferencer_->reset_realtime();
}

std::unique_ptr<RVCPipelineBase> RVCPipelineFactory::create(
    RvcMockConfig mock,
    const std::filesystem::path& models_dir,
    const std::filesystem::path& hubert_path,
    const std::optional<std::filesystem::path>& rmvpe_path,
    uint32_t input_sample_rate,
    uint32_t output_sample_rate,
    const std::string& device,
    bool half,
    RvcParameters parameters,
    const std::optional<std::filesystem::path>& realtime_hubert_path,
    const std::optional<std::filesystem::path>& realtime_rmvpe_path
) {
    auto model_manager = std::make_shared<ModelManager>(models_dir, device, half);
    if (!mock.generator) {
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
    }

    auto pipeline = std::make_unique<RealRVCPipeline>(
        model_manager, hubert_path, rmvpe_path,
        input_sample_rate, output_sample_rate,
        device, half, mock, std::move(parameters),
        realtime_hubert_path, realtime_rmvpe_path
    );
    spdlog::info(
        "RVC pipeline initialized: generator={}, hubert={}, rmvpe={} ({}Hz -> {}Hz)",
        mock.generator ? "mock" : "real", mock.hubert ? "mock" : "real",
        mock.rmvpe ? "mock" : "real", input_sample_rate, output_sample_rate
    );
    return pipeline;
}

} // namespace rvc
