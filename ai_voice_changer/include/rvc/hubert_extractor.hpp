// rvc/hubert_extractor.hpp
// ContentVec/HuBERT feature extraction via ONNX. Requires MOZART_ENABLE_ONNXRUNTIME.
#pragma once

#include "inference/infer_engine.hpp"

#include <memory>
#include <vector>

namespace mozart {

class HubertExtractor {
public:
    explicit HubertExtractor(std::shared_ptr<InferEngine> engine,
                             const std::string& onnx_path);
    ~HubertExtractor();

    // Input: 16kHz mono float32 samples.
    // Output: content features of shape [T, 768] (T = frames along time axis).
    // The exact ONNX I/O names are discovered at session creation; a default
    // convention of {"source"} in and {"hidden"} out is assumed and overridable.
    bool extract(std::span<const float> pcm16k,
                  std::vector<float>& out_feats,
                  std::vector<int64_t>& out_shape);

private:
    std::shared_ptr<InferEngine>            engine_;
    std::unique_ptr<InferSession>           session_;
    std::string                             in_name_  = "source";
    std::vector<std::string>                out_names_ = {"hidden"};
};

} // namespace mozart