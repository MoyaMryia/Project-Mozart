#pragma once
#include "infer_engine.hpp"
#include <vector>
#include <memory>
#include <string>

namespace mozart {

// RVC Generator (vocoder)
// Input: HuBERT features [1, T, 768] + F0 contour [1, T]
// Output: 48kHz audio waveform
class Generator {
public:
    explicit Generator(const std::string& model_path,
                       std::shared_ptr<InferEngine> engine);
    ~Generator();

    // Generate audio from features and F0
    // features: [1, T, 768] from HuBERT
    // f0: [1, T] pitch contour in Hz
    // Returns: audio samples at 48kHz
    std::vector<float> generate(const std::vector<float>& features,
                                const std::vector<float>& f0,
                                size_t feat_len);

    // Get output sample rate (48000 for RVC v2)
    size_t output_sample_rate() const { return 48000; }

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace mozart
