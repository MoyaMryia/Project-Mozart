#pragma once

#include <cstdint>
#include <vector>
#include <string>
#include <memory>
#include <filesystem>

#ifdef USE_ONNX
#include <onnxruntime_cxx_api.h>
#endif

namespace rvc {

class OnnxEngine {
public:
    OnnxEngine() = default;

    bool load(const std::filesystem::path& model_path);
    bool loaded() const { return session_ != nullptr; }

    std::vector<float> run(
        const std::vector<const char*>& input_names,
        const std::vector<std::vector<int64_t>>& input_shapes,
        const std::vector<std::vector<float>>& input_data,
        const std::vector<const char*>& output_names
    );

    void unload();

private:
#ifdef USE_ONNX
    std::unique_ptr<Ort::Env> env_;
    std::unique_ptr<Ort::SessionOptions> session_opts_;
    std::unique_ptr<Ort::Session> session_;
    Ort::MemoryInfo mem_info_{nullptr};
#endif
};

} // namespace rvc
