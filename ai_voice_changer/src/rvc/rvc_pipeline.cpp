// rvc/rvc_pipeline.cpp
#include "rvc/rvc_pipeline.hpp"
#include "rvc/model_loader.hpp"
#include "rvc/hubert_extractor.hpp"
#include "rvc/rmvpe_f0.hpp"
#include "rvc/generator.hpp"
#include "inference/infer_engine.hpp"
#include "inference/onnx_engine.hpp"
#include "utils/logging.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace mozart {

namespace {

// ---- Mock pipeline: linear 3x upsample, no neural inference --------------
class MockPipeline final : public RVCPipeline {
public:
    MockPipeline() { MOZART_INFO("RVCPipeline: mock mode (linear upsample)"); }

    void run(std::span<const float> pcm16k, const FrameMeta& /*meta*/,
             std::span<float> out48k) override {
        constexpr int kRatio = kOutputRate / 16000;  // 3
        // simple zero-order hold upsample + light lowpass via averaging window
        for (int i = 0; i < static_cast<int>(pcm16k.size()); ++i) {
            float v = pcm16k[i];
            for (int j = 0; j < kRatio; ++j) {
                int idx = i * kRatio + j;
                if (idx < static_cast<int>(out48k.size())) {
                    out48k[idx] = v;
                }
            }
        }
        // crude smoothing: 3-tap average
        for (int i = 1; i + 1 < static_cast<int>(out48k.size()); ++i) {
            out48k[i] = 0.25f * out48k[i - 1] + 0.5f * out48k[i] +
                        0.25f * out48k[i + 1];
        }
    }

    const char* name() const override { return "mock-pipeline"; }
};

#ifdef MOZART_ENABLE_ONNXRUNTIME
// ---- Real pipeline: HuBERT -> RMVPE -> Generator --------------------------
class RealPipeline final : public RVCPipeline {
public:
    RealPipeline(std::shared_ptr<InferEngine> engine,
                 const std::string& hubert_path,
                 const std::string& rmvpe_path,
                 const std::string& models_dir,
                 float index_rate)
        : engine_(std::move(engine)),
          models_(models_dir),
          index_rate_(index_rate) {
        hubert_ = std::make_unique<HubertExtractor>(engine_, hubert_path);
        rmvpe_  = std::make_unique<RmvpeF0>(engine_, rmvpe_path);
        // Try to load any available speaker model.
        auto models = models_.list_models();
        for (auto& m : models) {
            if (m.loaded) {
                activate(m.id);
                break;
            }
        }
    }

    bool activate(const std::string& id) {
        RVCModelInfo info;
        if (!models_.get_model(id, info)) return false;
        current_info_ = info;
        generator_ =
            std::make_unique<Generator>(engine_, info.generator_path.string(),
                                        info.sample_rate, info.spk_id, info.f0);
        models_.activate(id);
        return true;
    }

    void reset_state() override {
        // Real RVC streaming state reset goes here in a future iteration.
    }

    void run(std::span<const float> pcm16k, const FrameMeta& meta,
             std::span<float> out48k) override {
        if (!generator_) {
            // No speaker loaded: copy silence-derived upsample to output.
            MockPipeline fallback;
            fallback.run(pcm16k, meta, out48k);
            return;
        }

        std::vector<float> feats;
        std::vector<int64_t> feats_shape;
        if (!hubert_->extract(pcm16k, feats, feats_shape)) {
            MOZART_WARN("HuBERT extraction failed; emitting silence");
            std::fill(out48k.begin(), out48k.end(), 0.0f);
            return;
        }

        std::vector<float> f0;
        if (!rmvpe_->extract(pcm16k, f0)) {
            MOZART_WARN("RMVPE extraction failed");
            f0.assign(feats_shape[0], 0.0f);
        }

        std::vector<float> audio_out;
        int64_t n_out = 0;
        if (!generator_->run(feats, feats_shape, f0, f0, audio_out, n_out)) {
            std::fill(out48k.begin(), out48k.end(), 0.0f);
            return;
        }

        // write N samples, zero-pad if model returned fewer than output slot
        int copy = static_cast<int>(std::min<int64_t>(n_out,
                                                    static_cast<int64_t>(out48k.size())));
        std::copy(audio_out.begin(), audio_out.begin() + copy, out48k.begin());
        std::fill(out48k.begin() + copy, out48k.end(), 0.0f);
    }

    const char* name() const override { return "real-pipeline"; }

private:
    std::shared_ptr<InferEngine>  engine_;
    std::unique_ptr<HubertExtractor> hubert_;
    std::unique_ptr<RmvpeF0>        rmvpe_;
    std::unique_ptr<Generator>      generator_;
    ModelManager                     models_;
    RVCModelInfo                     current_info_{};
    float                            index_rate_;
};

#else

class RealPipeline final : public RVCPipeline {
public:
    RealPipeline(std::shared_ptr<InferEngine>,
                 const std::string&,
                 const std::string&,
                 const std::string&,
                 float) {
        throw std::runtime_error(
            "RealPipeline requires build with MOZART_ENABLE_ONNXRUNTIME");
    }
    void run(std::span<const float>, const FrameMeta&,
             std::span<float>) override {}
    const char* name() const override { return "real-pipeline-unavailable"; }
};

#endif  // MOZART_ENABLE_ONNXRUNTIME

}  // namespace

std::unique_ptr<RVCPipeline> make_pipeline(
    bool mock_mode,
    const std::string& assets_dir,
    const std::string& hubert_path,
    const std::string& rmvpe_path,
    const std::string& models_dir,
    const std::string& device,
    float index_rate) {
    (void)assets_dir;
    if (mock_mode) {
        return std::make_unique<MockPipeline>();
    }

#ifdef MOZART_ENABLE_ONNXRUNTIME
    auto engine = std::make_shared<OnnxEngine>(device);
    (void)rmvpe_path; (void)models_dir; (void)index_rate;
    return std::make_unique<RealPipeline>(
        engine, hubert_path, rmvpe_path, models_dir, index_rate);
#else
    (void)hubert_path; (void)rmvpe_path; (void)models_dir;
    (void)device; (void)index_rate;
    throw std::runtime_error(
        "Real pipeline unavailable: rebuild with -DMOZART_ENABLE_ONNXRUNTIME=ON");
#endif
}

}  // namespace mozart