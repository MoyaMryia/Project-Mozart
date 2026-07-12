#include <mozart/rvc/rvc_pipeline.hpp>
#include <mozart/rvc/hubert_extractor.hpp>
#include <mozart/rvc/rmvpe_f0.hpp>
#include <mozart/rvc/generator.hpp>
#include <mozart/rvc/infer_engine.hpp>
#include <mozart/utils/logging.hpp>
#include <cmath>

namespace mozart {

// ── MockRVCPipeline ───────────────────────────────────────────────────────────
void MockRVCPipeline::process(const FrameBuf& in, OutputFrameBuf& out) {
    // Simple 3x upsampling with linear interpolation (16kHz -> 48kHz)
    constexpr size_t kIn = 320;
    constexpr size_t kOut = 960;

    for (size_t i = 0; i < kOut; ++i) {
        double pos = static_cast<double>(i) / 3.0;
        size_t idx = static_cast<size_t>(pos);
        double frac = pos - idx;

        if (idx >= kIn - 1) {
            out.samples[i] = in.samples[kIn - 1];
        } else {
            out.samples[i] = static_cast<float>(
                in.samples[idx] * (1.0 - frac) + in.samples[idx + 1] * frac
            );
        }
    }
}

// ── RealRVCPipeline ───────────────────────────────────────────────────────────
struct RealRVCPipeline::Impl {
    std::shared_ptr<InferEngine> engine;
    std::unique_ptr<HuBERTExtractor> hubert;
    std::unique_ptr<RMVPEF0> rmvpe;
    std::unique_ptr<Generator> generator;
    RVCParams params;

    // Streaming state
    uint8_t last_segment_id = 0;
};

RealRVCPipeline::RealRVCPipeline(const Config& cfg) : impl_(std::make_unique<Impl>()) {
    impl_->params = cfg.params;

    // Create ONNX engine
    OnnxEngine::Config engine_cfg;
    engine_cfg.use_cuda = true;
    impl_->engine = std::make_shared<OnnxEngine>(engine_cfg);

    // Load models
    impl_->hubert = std::make_unique<HuBERTExtractor>(cfg.hubert_path, impl_->engine);
    impl_->rmvpe = std::make_unique<RMVPEF0>(cfg.model_dir + "/rmvpe.onnx", impl_->engine);
    impl_->generator = std::make_unique<Generator>(cfg.model_dir + "/generator.onnx", impl_->engine);

    MOZART_LOG_INFO("Real RVC pipeline initialized with model: {}", cfg.model_dir);
}

RealRVCPipeline::~RealRVCPipeline() = default;

void RealRVCPipeline::process(const FrameBuf& in, OutputFrameBuf& out) {
    // Check for segment change
    if (in.meta.segment_id != impl_->last_segment_id) {
        reset_state();
        impl_->last_segment_id = in.meta.segment_id;
    }

    // Convert to vector for processing
    std::vector<float> audio(in.samples.begin(), in.samples.end());

    // Extract F0
    auto f0 = impl_->rmvpe->extract(audio, 16000, impl_->params.pitch_shift);
    if (impl_->params.filter_radius > 0) {
        impl_->rmvpe->median_filter(f0, impl_->params.filter_radius);
    }

    // Extract HuBERT features
    auto features = impl_->hubert->extract(audio, 16000);

    // Generate audio
    auto output = impl_->generator->generate(features, f0, features.size() / 768);

    // Copy to output buffer
    size_t copy_len = std::min(output.size(), out.samples.size());
    std::memcpy(out.samples.data(), output.data(), copy_len * sizeof(float));
}

void RealRVCPipeline::reset_state() {
    MOZART_LOG_DEBUG("Resetting pipeline state for new segment");
    // TODO: Reset any streaming buffers in HuBERT/Generator
}

// ── Factory ───────────────────────────────────────────────────────────────────
std::unique_ptr<RVCPipeline> create_pipeline(
    bool mock_mode,
    const RealRVCPipeline::Config& cfg) {
    if (mock_mode) {
        MOZART_LOG_INFO("Using mock RVC pipeline");
        return std::make_unique<MockRVCPipeline>();
    } else {
        MOZART_LOG_INFO("Using real RVC pipeline");
        return std::make_unique<RealRVCPipeline>(cfg);
    }
}

}  // namespace mozart
