#pragma once
#include "infer_engine.hpp"
#include <vector>
#include <memory>
#include <string>

namespace mozart {

// RMVPE F0 (pitch) extractor
// Input: 16kHz audio waveform
// Output: F0 contour (pitch in Hz per frame)
class RMVPEF0 {
public:
    enum class Method {
        RMVPE,   // Deep learning based (best quality)
        HARVEST, // WORLD Harvest (CPU fallback)
        PM,      // Praat PM (fastest, lowest quality)
    };

    explicit RMVPEF0(const std::string& model_path,
                     std::shared_ptr<InferEngine> engine,
                     Method method = Method::RMVPE);
    ~RMVPEF0();

    // Extract F0 from audio samples (16kHz float32)
    // Returns F0 values in Hz, one per frame (hop=160 for 16kHz)
    std::vector<float> extract(const std::vector<float>& audio,
                               size_t sample_rate,
                               int pitch_shift = 0);

    // Apply median filter to F0 contour
    void median_filter(std::vector<float>& f0, int radius);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace mozart
