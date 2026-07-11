// rvc/generator.hpp
// RVC Generator (Synthesizer) inference via ONNX. Requires MOZART_ENABLE_ONNXRUNTIME.
//
// Inputs conventionally:
//   "phone"  : content features [T, 768]
//   "phone_lengths" : [1]
//   "pitch"  : coarse pitch tokens [T]      (when model has f0)
//   "pitchf" : fine pitch in Hz [T]         (when model has f0)
//   "sid"    : speaker id [1]
// Output:
//   "audio"  : waveform [1, 1, T' * hop]
//
// The exact names are model-specific; this wrapper discovers them at
// session creation and falls back to the convention above.
#pragma once

#include "inference/infer_engine.hpp"

#include <memory>
#include <vector>

namespace mozart {

class Generator {
public:
    explicit Generator(std::shared_ptr<InferEngine> engine,
                       const std::string& onnx_path,
                       int sample_rate = 48000,
                       int spk_id = 0,
                       bool has_f0 = true);
    ~Generator();

    // Runs the generator. Output is resampled to the generator's native rate.
    bool run(std::span<const float> content_feats,
             const std::vector<int64_t>& content_shape,
             std::span<const float> f0,
             std::span<const float> pitchf,
             std::vector<float>& out_audio,
             int64_t& out_samples);

private:
    std::shared_ptr<InferEngine> engine_;
    std::unique_ptr<InferSession> session_;
    int     spk_id_;
    bool    has_f0_;
    int     sample_rate_;
};

} // namespace mozart