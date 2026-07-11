// rvc/rmvpe_f0.hpp
// RMVPE pitch (F0) extraction via ONNX. Requires MOZART_ENABLE_ONNXRUNTIME.
#pragma once

#include "inference/infer_engine.hpp"

#include <memory>
#include <vector>

namespace mozart {

class RmvpeF0 {
public:
    explicit RmvpeF0(std::shared_ptr<InferEngine> engine,
                     const std::string& onnx_path);
    ~RmvpeF0();

    // Input: 16kHz mono float32 samples.
    // Output: f0 contour (Hz). Length depends on the model's frame hop.
    bool extract(std::span<const float> pcm16k,
                 std::vector<float>& out_f0);

private:
    std::shared_ptr<InferEngine> engine_;
    std::unique_ptr<InferSession> session_;
};

} // namespace mozart