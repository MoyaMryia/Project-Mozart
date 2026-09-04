#pragma once

// trt_engine.hpp — TensorRT 10 直载引擎（绕过 ONNX Runtime）
// ============================================================================
// 与 OnnxEngine 同接口（IEngine），由 make_engine() 在发现 <stem>.engine
// 时选用。固定和 optimization-profile 动态引擎都支持，运行时：
//   deserialize → setInputShape → 按需扩容绑定 → enqueueV3 → D2H 回拷。
// 输入类型 float / int64 / int32（int64 输入若引擎要 int32 会自动降转换）。
#include "rvc/onnx_engine.hpp"

#include <unordered_map>

namespace rvc {

#ifdef USE_TENSORRT

class TrtEngine final : public IEngine {
public:
    TrtEngine() = default;
    ~TrtEngine() override { unload(); }

    bool load(const std::filesystem::path& engine_path) override;
    bool loaded() const override { return context_ != nullptr; }
    std::optional<OnnxInput::Type> input_type(const std::string& name) const override;

    std::vector<float> run(const std::vector<OnnxInput>& inputs,
                           const std::vector<const char*>& output_names) override;
    std::vector<float> run(
        const std::vector<const char*>& input_names,
        const std::vector<std::vector<int64_t>>& input_shapes,
        const std::vector<std::vector<float>>& input_data,
        const std::vector<const char*>& output_names) override;

    // 引擎某输入的静态形状（load 后有效）；无此张量返回空
    std::vector<int64_t> input_shape(const std::string& name) const override;

    void unload() override;

private:
    struct Tensor {
        std::string name;
        bool is_input = false;
        int32_t dtype = 0;               // nvinfer1::DataType
        std::vector<int64_t> dims;
        size_t elems = 0;
        size_t bytes = 0;
        size_t capacity_bytes = 0;
        void* dev = nullptr;
    };

    static std::string dtype_name(int32_t dtype);

    void* engine_ = nullptr;             // nvinfer1::ICudaEngine
    void* context_ = nullptr;            // nvinfer1::IExecutionContext
    void* stream_ = nullptr;             // cudaStream_t
    std::vector<Tensor> tensors_;
    std::unordered_map<std::string, OnnxInput::Type> input_types_;
};

#else // 无 TensorRT：不可用桩

class TrtEngine final : public IEngine {
public:
    bool load(const std::filesystem::path& path) override {
        (void)path;
        return false;
    }
    bool loaded() const override { return false; }
    std::optional<OnnxInput::Type> input_type(const std::string&) const override {
        return std::nullopt;
    }
    std::vector<float> run(const std::vector<OnnxInput>&,
                           const std::vector<const char*>&) override {
        throw std::runtime_error("TensorRT not compiled in (USE_TENSORRT=OFF)");
    }
    std::vector<float> run(const std::vector<const char*>&,
                           const std::vector<std::vector<int64_t>>&,
                           const std::vector<std::vector<float>>&,
                           const std::vector<const char*>&) override {
        throw std::runtime_error("TensorRT not compiled in (USE_TENSORRT=OFF)");
    }
    void unload() override {}
};

#endif // USE_TENSORRT

} // namespace rvc
