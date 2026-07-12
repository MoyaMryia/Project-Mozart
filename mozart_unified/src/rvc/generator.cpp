#include <mozart/rvc/generator.hpp>
#include <mozart/utils/logging.hpp>

namespace mozart {

struct Generator::Impl {
    std::unique_ptr<InferSession> session;
};

Generator::Generator(const std::string& model_path,
                     std::shared_ptr<InferEngine> engine)
    : impl_(std::make_unique<Impl>()) {
    impl_->session = engine->create_session(model_path);
    MOZART_LOG_INFO("RVC Generator loaded from {}", model_path);
}

Generator::~Generator() = default;

std::vector<float> Generator::generate(const std::vector<float>& features,
                                       const std::vector<float>& f0,
                                       size_t feat_len) {
    // TODO: Implement actual audio generation
    // For now, return silence at 48kHz
    // RVC v2 generator outputs 48kHz, with ~3x upsampling from 16kHz input
    size_t output_len = feat_len * 960;  // 20ms frames -> 960 samples each
    return std::vector<float>(output_len, 0.0f);
}

}  // namespace mozart
