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

// 推理引擎抽象：OnnxEngine（CPU/CUDA EP）与 TrtEngine（TensorRT 直载）同接口
class IEngine {
public:
    virtual ~IEngine() = default;
    virtual bool load(const std::filesystem::path& model_path) = 0;
    virtual bool loaded() const = 0;
    virtual std::optional<OnnxInput::Type> input_type(const std::string& name) const = 0;
    virtual std::vector<float> run(
        const std::vector<const char*>& input_names,
        const std::vector<std::vector<int64_t>>& input_shapes,
        const std::vector<std::vector<float>>& input_data,
        const std::vector<const char*>& output_names) = 0;
    virtual std::vector<float> run(const std::vector<OnnxInput>& inputs,
                                   const std::vector<const char*>& output_names) = 0;
    // 引擎某输入的静态形状；动态/未知返回空（用于 TRT 固定形状校验）
    virtual std::vector<int64_t> input_shape(const std::string& name) const { return {}; }
    virtual void unload() = 0;
};

// 工厂：model_path 为 .onnx；同目录 <stem>.engine 存在时优先 TensorRT，
// 否则回退 ONNX Runtime。返回已加载的引擎（失败返回 nullptr）。
std::unique_ptr<IEngine> make_engine(const std::filesystem::path& model_path);

class OnnxEngine final : public IEngine {
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
