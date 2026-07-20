#include "rvc/onnx_engine.hpp"
#include <spdlog/spdlog.h>
#include <stdexcept>
#include <cstring>

namespace rvc {

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

        session_ = std::make_unique<Ort::Session>(
            *env_, model_path.c_str(), *session_opts_
        );

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

std::vector<float> OnnxEngine::run(
    const std::vector<const char*>& input_names,
    const std::vector<std::vector<int64_t>>& input_shapes,
    const std::vector<std::vector<float>>& input_data,
    const std::vector<const char*>& output_names
) {
    if (!session_) {
        throw std::runtime_error("ONNX engine not loaded");
    }

    std::vector<Ort::Value> input_tensors;
    for (size_t i = 0; i < input_names.size(); ++i) {
        size_t elem_count = 1;
        for (auto d : input_shapes[i]) elem_count *= static_cast<size_t>(d);

        input_tensors.push_back(Ort::Value::CreateTensor<float>(
            mem_info_,
            const_cast<float*>(input_data[i].data()),
            elem_count,
            input_shapes[i].data(),
            input_shapes[i].size()
        ));
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
}

#else // USE_ONNX not defined — stub implementation

bool OnnxEngine::load(const std::filesystem::path& model_path) {
    spdlog::warn("ONNX Runtime not compiled in (USE_ONNX=OFF); stub load: {}",
                 model_path.string());
    return std::filesystem::exists(model_path);
}

std::vector<float> OnnxEngine::run(
    const std::vector<const char*>&,
    const std::vector<std::vector<int64_t>>&,
    const std::vector<std::vector<float>>&,
    const std::vector<const char*>&
) {
    throw std::runtime_error("ONNX Runtime not available (USE_ONNX=OFF)");
}

void OnnxEngine::unload() {}

#endif

} // namespace rvc
