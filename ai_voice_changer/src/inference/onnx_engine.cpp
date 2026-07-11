// inference/onnx_engine.cpp
#include "inference/onnx_engine.hpp"
#include "utils/logging.hpp"

#include <onnxruntime_cxx_api.h>

#include <stdexcept>
#include <utility>

namespace mozart {

namespace {

class OnnxSession final : public InferSession {
public:
    OnnxSession(Ort::Env& env,
               const std::string& path,
               const std::string& device) {
        Ort::SessionOptions opts;
        opts.SetIntraOpNumThreads(1);
        opts.SetGraphOptimizationLevel(
            GraphOptimizationLevel::ORT_ENABLE_EXTENDED);

        if (device == "cuda") {
            try {
                OrtCUDAProviderOptions cuda_opts{};
                cuda_opts.device_id             = 0;
                cuda_opts.arena_extend_strategy = 0;
                cuda_opts.gpu_mem_limit         = static_cast<size_t>(2ULL << 30);
                cuda_opts.tunable_op_enable     = 0;
                cuda_opts.tunable_op_max_tuning_iterations = 1;
                opts.AppendExecutionProvider_CUDA(cuda_opts);
                MOZART_INFO("OnnxEngine: enabled CUDA EP");
            } catch (const Ort::Exception& e) {
                MOZART_WARN("OnnxEngine: CUDA EP append failed (%s); CPU fallback",
                            e.what());
            }
        }

        session_ = std::make_unique<Ort::Session>(env, path.c_str(), opts);
    }

    void run(const std::vector<std::pair<std::string, Tensor>>& inputs,
             std::vector<std::pair<std::string, Tensor>>&       outputs) override {
        auto mem_info = Ort::MemoryInfo::CreateCpu(
            OrtArenaAllocator, OrtMemTypeDefault);

        std::vector<Ort::Value>  in_tensors;
        std::vector<const char*> in_names;
        in_tensors.reserve(inputs.size());
        in_names.reserve(inputs.size());
        for (const auto& [name, t] : inputs) {
            in_names.push_back(name.c_str());
            in_tensors.emplace_back(Ort::Value::CreateTensor<float>(
                mem_info,
                const_cast<float*>(t.data.data()),
                t.data.size(),
                t.shape.data(),
                t.shape.size()));
        }

        std::vector<const char*> out_names;
        out_names.reserve(outputs.size());
        for (auto& [name, _] : outputs) out_names.push_back(name.c_str());

        auto out_tensors = session_->Run(
            Ort::RunOptions{nullptr},
            in_names.data(), in_tensors.data(), in_tensors.size(),
            out_names.data(), out_names.size());

        for (size_t i = 0; i < outputs.size(); ++i) {
            auto& info = out_tensors[i].GetTensorTypeAndShapeInfo();
            auto  shape = info.GetShape();
            size_t count = 1;
            for (auto s : shape) {
                if (s < 0) s = 1;
                count *= static_cast<size_t>(s);
            }
            auto* p = out_tensors[i].GetTensorMutableData<float>();
            outputs[i].second.shape = shape;
            outputs[i].second.data.assign(p, p + count);
        }
    }

private:
    std::unique_ptr<Ort::Session> session_;
};

}  // namespace

struct OnnxEngine::Impl {
    Ort::Env env{ORT_LOGGING_LEVEL_WARNING, "mozart-post"};
};

OnnxEngine::OnnxEngine(const std::string& device)
    : device_(device), impl_(std::make_unique<Impl>()) {}

OnnxEngine::~OnnxEngine() = default;

std::unique_ptr<InferSession> OnnxEngine::create_session(const std::string& onnx_path) {
    return std::make_unique<OnnxSession>(impl_->env, onnx_path, device_);
}

}  // namespace mozart