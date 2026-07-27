#pragma once

#include <cstdint>
#include <vector>
#include <string>
#include <memory>
#include <filesystem>
#include <optional>
#include <unordered_map>

#ifdef USE_ONNX
#include <onnxruntime_cxx_api.h>
#endif

namespace rvc {

struct OnnxInput {
    enum class Type { Float, Int64 };

    const char* name = nullptr;
    std::vector<int64_t> shape;
    Type type = Type::Float;
    std::vector<float> floats;
    std::vector<int64_t> int64s;
};

class OnnxEngine {
public:
    OnnxEngine() = default;

    bool load(const std::filesystem::path& model_path);
    bool loaded() const { return session_ != nullptr; }
    std::optional<OnnxInput::Type> input_type(const std::string& name) const;

    std::vector<float> run(
        const std::vector<const char*>& input_names,
        const std::vector<std::vector<int64_t>>& input_shapes,
        const std::vector<std::vector<float>>& input_data,
        const std::vector<const char*>& output_names
    );
    std::vector<float> run(const std::vector<OnnxInput>& inputs,
                           const std::vector<const char*>& output_names);

    void unload();

private:
#ifdef USE_ONNX
    std::unique_ptr<Ort::Env> env_;
    std::unique_ptr<Ort::SessionOptions> session_opts_;
    std::unique_ptr<Ort::Session> session_;
    Ort::MemoryInfo mem_info_{nullptr};
#endif
    std::unordered_map<std::string, OnnxInput::Type> input_types_;
};

} // namespace rvc
