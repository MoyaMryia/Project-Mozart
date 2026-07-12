#include <mozart/rvc/hubert_extractor.hpp>
#include <mozart/utils/logging.hpp>

namespace mozart {

struct HuBERTExtractor::Impl {
    std::unique_ptr<InferSession> session;
};

HuBERTExtractor::HuBERTExtractor(const std::string& model_path,
                                 std::shared_ptr<InferEngine> engine)
    : impl_(std::make_unique<Impl>()) {
    impl_->session = engine->create_session(model_path);
    MOZART_LOG_INFO("HuBERT extractor loaded from {}", model_path);
}

HuBERTExtractor::~HuBERTExtractor() = default;

std::vector<float> HuBERTExtractor::extract(const std::vector<float>& audio, size_t sample_rate) {
    // TODO: Implement actual HuBERT feature extraction
    // For now, return dummy features [1, T, 768]
    size_t T = audio.size() / 320;  // Rough estimate
    if (T == 0) T = 1;
    return std::vector<float>(T * 768, 0.0f);
}

}  // namespace mozart
