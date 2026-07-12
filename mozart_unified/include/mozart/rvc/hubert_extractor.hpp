#pragma once
#include "infer_engine.hpp"
#include <vector>
#include <memory>
#include <string>

namespace mozart {

// HuBERT feature extractor
// Input: 16kHz audio waveform
// Output: [1, T, 768] feature tensor
class HuBERTExtractor {
public:
    explicit HuBERTExtractor(const std::string& model_path,
                             std::shared_ptr<InferEngine> engine);
    ~HuBERTExtractor();

    // Extract features from audio samples (16kHz float32)
    std::vector<float> extract(const std::vector<float>& audio, size_t sample_rate);

    // Get feature dimension (768 for HuBERT base)
    size_t feature_dim() const { return 768; }

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace mozart
