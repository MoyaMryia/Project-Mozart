#pragma once
#include <vector>
#include <string>
#include <memory>

namespace mozart {

// Abstract inference session
class InferSession {
public:
    virtual ~InferSession() = default;

    // Run inference on input tensor, return output tensor
    virtual std::vector<float> run(const std::vector<float>& input,
                                   const std::vector<int64_t>& input_shape) = 0;

    // Get input/output names
    virtual std::string input_name() const = 0;
    virtual std::string output_name() const = 0;
};

// Abstract inference engine
class InferEngine {
public:
    virtual ~InferEngine() = default;

    // Create a session from model file
    virtual std::unique_ptr<InferSession> create_session(const std::string& model_path) = 0;

    // Get engine name
    virtual const char* name() const = 0;
};

// ONNX Runtime engine (CUDA EP on Jetson)
class OnnxEngine : public InferEngine {
public:
    struct Config {
        bool use_cuda = true;
        int cuda_device_id = 0;
        int intra_op_threads = 0;  // 0 = auto
        int inter_op_threads = 0;
    };

    explicit OnnxEngine(const Config& cfg = {});
    ~OnnxEngine();

    std::unique_ptr<InferSession> create_session(const std::string& model_path) override;
    const char* name() const override { return "onnx"; }

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace mozart
