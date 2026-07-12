#include <mozart/rvc/infer_engine.hpp>
#include <mozart/utils/logging.hpp>

#ifdef MOZART_ENABLE_ONNXRUNTIME
#include <onnxruntime_cxx_api.h>
#endif

namespace mozart {

// ── OnnxEngine ────────────────────────────────────────────────────────────────
#ifdef MOZART_ENABLE_ONNXRUNTIME

struct OnnxEngine::Impl {
    Ort::Env env;
    Ort::SessionOptions session_options;
    Config cfg;
};

OnnxEngine::OnnxEngine(const Config& cfg) : impl_(std::make_unique<Impl>()) {
    impl_->cfg = cfg;
    impl_->env = Ort::Env(ORT_LOGGING_LEVEL_WARNING, "mozart");

    impl_->session_options.SetIntraOpNumThreads(cfg.intra_op_threads);
    impl_->session_options.SetInterOpNumThreads(cfg.inter_op_threads);
    impl_->session_options.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);

    if (cfg.use_cuda) {
        OrtCUDAProviderOptions cuda_opts;
        cuda_opts.device_id = cfg.cuda_device_id;
        impl_->session_options.AppendExecutionProvider_CUDA(cuda_opts);
        MOZART_LOG_INFO("ONNX Runtime initialized with CUDA EP (device {})", cfg.cuda_device_id);
    } else {
        MOZART_LOG_INFO("ONNX Runtime initialized with CPU EP");
    }
}

OnnxEngine::~OnnxEngine() = default;

class OnnxSession : public InferSession {
public:
    OnnxSession(Ort::Session session, std::string input_name, std::string output_name)
        : session_(std::move(session))
        , input_name_(std::move(input_name))
        , output_name_(std::move(output_name)) {}

    std::vector<float> run(const std::vector<float>& input,
                          const std::vector<int64_t>& input_shape) override {
        auto memory_info = Ort::MemoryInfo::CreateCpu(OrtArenaAllocator, OrtMemTypeDefault);
        auto input_tensor = Ort::Value::CreateTensor<float>(
            memory_info, const_cast<float*>(input.data()), input.size(),
            input_shape.data(), input_shape.size()
        );

        auto output_tensors = session_.Run(
            Ort::RunOptions{nullptr},
            input_name_.c_str(),
            &input_tensor, 1,
            output_name_.c_str(), 1
        );

        auto& output_tensor = output_tensors[0];
        auto* output_data = output_tensor.GetTensorMutableData<float>();
        auto output_shape = output_tensor.GetTensorTypeAndShapeInfo().GetShape();

        size_t output_size = 1;
        for (auto dim : output_shape) output_size *= dim;

        return std::vector<float>(output_data, output_data + output_size);
    }

    std::string input_name() const override { return input_name_; }
    std::string output_name() const override { return output_name_; }

private:
    Ort::Session session_;
    std::string input_name_;
    std::string output_name_;
};

std::unique_ptr<InferSession> OnnxEngine::create_session(const std::string& model_path) {
    Ort::Session session(impl_->env, model_path.c_str(), impl_->session_options);

    // Get input/output names
    Ort::AllocatorWithDefaultOptions allocator;
    auto input_name = session.GetInputNameAllocated(0, allocator);
    auto output_name = session.GetOutputNameAllocated(0, allocator);

    return std::make_unique<OnnxSession>(
        std::move(session),
        input_name.get(),
        output_name.get()
    );
}

#else  // MOZART_ENABLE_ONNXRUNTIME

struct OnnxEngine::Impl {};

OnnxEngine::OnnxEngine(const Config&) : impl_(std::make_unique<Impl>()) {
    MOZART_LOG_WARN("ONNX Runtime not enabled, OnnxEngine will not work");
}

OnnxEngine::~OnnxEngine() = default;

std::unique_ptr<InferSession> OnnxEngine::create_session(const std::string&) {
    throw std::runtime_error("ONNX Runtime not enabled");
}

#endif  // MOZART_ENABLE_ONNXRUNTIME

}  // namespace mozart
