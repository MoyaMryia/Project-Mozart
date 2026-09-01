#include "rvc/onnx_engine.hpp"
#include "rvc/trt_engine.hpp"
#include <spdlog/spdlog.h>
#include <stdexcept>
#include <cstring>

namespace rvc {

std::unique_ptr<IEngine> make_engine(const std::filesystem::path& model_path) {
    // 约定：模型 X.onnx 旁存在 X.engine 时优先 TensorRT 直载（GPU）
    if (model_path.extension() == ".onnx") {
        auto engine_path = model_path;
        engine_path.replace_extension(".engine");
        if (std::filesystem::exists(engine_path)) {
            auto trt = std::make_unique<TrtEngine>();
            if (trt->load(engine_path)) {
                spdlog::info("Engine backend: TensorRT (GPU) → {}", engine_path.filename().string());
                return trt;
            }
            spdlog::warn("TRT engine {} 加载失败，回退 ONNX", engine_path.string());
        }
    }
    auto onnx = std::make_unique<OnnxEngine>();
    if (onnx->load(model_path)) {
        spdlog::info("Engine backend: ONNX Runtime ({}) → {}",
#ifdef USE_CUDA_EP
                     "CUDA EP requested",
#else
                     "CPU",
#endif
                     model_path.filename().string());
        return onnx;
    }
    return nullptr;
}

#ifdef USE_ONNX

bool OnnxEngine::load(const std::filesystem::path& model_path) {
    if (!std::filesystem::exists(model_path)) {
        spdlog::error("ONNX model not found: {}", model_path.string());
        return false;
    }

    try {
        env_ = std::make_unique<Ort::Env>(ORT_LOGGING_LEVEL_WARNING, "rvc_backend");
        session_opts_ = std::make_unique<Ort::SessionOptions>();
        session_opts_->SetIntraOpNumThreads(2);
        session_opts_->SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);

#ifdef USE_CUDA_EP
        // Opt-in GPU path: requires an ONNX Runtime build that ships the CUDA EP.
        // Enable with -DUSE_CUDA_EP=ON once a CUDA-enabled libonnxruntime is installed.
        // Default build (CPU-only ORT) must NOT define this, or linking fails.
        Ort::ThrowOnError(OrtSessionOptionsAppendExecutionProvider_CUDA(
            session_opts_.get(), /*device_id=*/0));
        spdlog::info("CUDA execution provider attached for {}", model_path.string());
#endif

        session_ = std::make_unique<Ort::Session>(
            *env_, model_path.c_str(), *session_opts_
        );
        input_types_.clear();
        for (size_t index = 0; index < session_->GetInputCount(); ++index) {
            const auto name = session_->GetInputNameAllocated(index, Ort::AllocatorWithDefaultOptions{});
            const auto type = session_->GetInputTypeInfo(index).GetTensorTypeAndShapeInfo().GetElementType();
            if (type == ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT) {
                input_types_[name.get()] = OnnxInput::Type::Float;
            } else if (type == ONNX_TENSOR_ELEMENT_DATA_TYPE_INT64) {
                input_types_[name.get()] = OnnxInput::Type::Int64;
            } else {
                throw std::runtime_error("unsupported ONNX input type for " + std::string(name.get()));
            }
        }

        mem_info_ = Ort::MemoryInfo::CreateCpu(
            OrtArenaAllocator, OrtMemTypeDefault
        );

        spdlog::info("ONNX engine loaded: {} ({} inputs, {} outputs)",
            model_path.filename().string(),
            session_->GetInputCount(),
            session_->GetOutputCount()
        );
        return true;

    } catch (const Ort::Exception& e) {
        spdlog::error("ONNX load failed: {} - {}", model_path.string(), e.what());
        unload();
        return false;
    }
}

std::optional<OnnxInput::Type> OnnxEngine::input_type(const std::string& name) const {
    const auto input = input_types_.find(name);
    return input == input_types_.end() ? std::nullopt : std::optional<OnnxInput::Type>(input->second);
}

std::vector<float> OnnxEngine::run(
    const std::vector<const char*>& input_names,
    const std::vector<std::vector<int64_t>>& input_shapes,
    const std::vector<std::vector<float>>& input_data,
    const std::vector<const char*>& output_names
) {
    std::vector<OnnxInput> inputs;
    inputs.reserve(input_names.size());
    for (size_t i = 0; i < input_names.size(); ++i) {
        inputs.push_back({input_names[i], input_shapes[i], OnnxInput::Type::Float, input_data[i], {}});
    }
    return run(inputs, output_names);
}

std::vector<float> OnnxEngine::run(const std::vector<OnnxInput>& inputs,
                                   const std::vector<const char*>& output_names) {
    if (!session_) {
        throw std::runtime_error("ONNX engine not loaded");
    }

    std::vector<Ort::Value> input_tensors;
    std::vector<const char*> input_names;
    input_tensors.reserve(inputs.size());
    input_names.reserve(inputs.size());
    for (const auto& input : inputs) {
        size_t elem_count = 1;
        for (const auto dimension : input.shape) elem_count *= static_cast<size_t>(dimension);
        input_names.push_back(input.name);
        if (input.type == OnnxInput::Type::Float) {
            if (input.floats.size() != elem_count) throw std::invalid_argument("ONNX float input shape mismatch");
            input_tensors.push_back(Ort::Value::CreateTensor<float>(
                mem_info_, const_cast<float*>(input.floats.data()), elem_count,
                input.shape.data(), input.shape.size()));
        } else {
            if (input.int64s.size() != elem_count) throw std::invalid_argument("ONNX int64 input shape mismatch");
            input_tensors.push_back(Ort::Value::CreateTensor<int64_t>(
                mem_info_, const_cast<int64_t*>(input.int64s.data()), elem_count,
                input.shape.data(), input.shape.size()));
        }
    }

    auto output_tensors = session_->Run(
        Ort::RunOptions{nullptr},
        input_names.data(), input_tensors.data(), input_tensors.size(),
        output_names.data(), output_names.size()
    );

    auto& out = output_tensors[0];
    auto* out_data = out.GetTensorMutableData<float>();
    auto out_shape = out.GetTensorTypeAndShapeInfo().GetShape();
    size_t out_elem_count = 1;
    for (auto d : out_shape) out_elem_count *= static_cast<size_t>(d);

    return std::vector<float>(out_data, out_data + out_elem_count);
}

void OnnxEngine::unload() {
    session_.reset();
    session_opts_.reset();
    env_.reset();
    input_types_.clear();
}

#else // USE_ONNX not defined — stub implementation

bool OnnxEngine::load(const std::filesystem::path& model_path) {
    spdlog::warn("ONNX Runtime not compiled in (USE_ONNX=OFF); stub load: {}",
                 model_path.string());
    return std::filesystem::exists(model_path);
}

std::optional<OnnxInput::Type> OnnxEngine::input_type(const std::string&) const {
    return std::nullopt;
}

std::vector<float> OnnxEngine::run(
    const std::vector<const char*>&,
    const std::vector<std::vector<int64_t>>&,
    const std::vector<std::vector<float>>&,
    const std::vector<const char*>&
) {
    throw std::runtime_error("ONNX Runtime not available (USE_ONNX=OFF)");
}

std::vector<float> OnnxEngine::run(const std::vector<OnnxInput>&,
                                   const std::vector<const char*>&) {
    throw std::runtime_error("ONNX Runtime not available (USE_ONNX=OFF)");
}

void OnnxEngine::unload() {}

#endif

} // namespace rvc
