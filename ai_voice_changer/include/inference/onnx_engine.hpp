// inference/onnx_engine.hpp
// ONNX Runtime CUDA EP engine for Jetson. Requires MOZART_ENABLE_ONNXRUNTIME.
#pragma once

#include "inference/infer_engine.hpp"

#include <memory>
#include <string>

namespace mozart {

class OnnxEngine final : public InferEngine {
public:
    // device: "cuda" or "cpu". When "cuda", uses CUDAExecutionProvider.
    explicit OnnxEngine(const std::string& device = "cuda");
    ~OnnxEngine() override;

    OnnxEngine(const OnnxEngine&) = delete;
    OnnxEngine& operator=(const OnnxEngine&) = delete;

    std::unique_ptr<InferSession> create_session(
        const std::string& onnx_path) override;

    const char* name() const override { return "onnxruntime"; }

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
    std::string device_;
};

} // namespace mozart