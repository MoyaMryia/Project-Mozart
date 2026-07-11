// inference/infer_engine.hpp
// Abstract inference engine session. Each session wraps a single ONNX model.
#pragma once

#include <cstddef>
#include <memory>
#include <span>
#include <string>
#include <vector>

namespace mozart {

// Runtime tensor: shape + contiguous float data (NCHW or whatever the model expects).
struct Tensor {
    std::vector<int64_t> shape;
    std::vector<float>   data;  // device-resident when supported; here as host mirror
};

class InferSession {
public:
    virtual ~InferSession() = default;

    // Run forward. inputs/outputs keyed by ONNX I/O name.
    virtual void run(const std::vector<std::pair<std::string, Tensor>>& inputs,
                     std::vector<std::pair<std::string, Tensor>>&       outputs) = 0;
};

class InferEngine {
public:
    virtual ~InferEngine() = default;

    // Load an ONNX model file and return a session.
    virtual std::unique_ptr<InferSession> create_session(
        const std::string& onnx_path) = 0;

    virtual const char* name() const = 0;
};

} // namespace mozart