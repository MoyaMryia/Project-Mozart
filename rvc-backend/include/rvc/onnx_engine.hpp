#pragma once

#include <cstdint>
#include <vector>
#include <string>
#include <memory>
#include <filesystem>
#include <optional>
#include <unordered_map>
#include <map>
#include <mutex>
#include <stdexcept>

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
    // 默认路径保留 CPU/ONNX 兼容性。TensorRT 可直接连接两个设备张量。
    virtual std::vector<float> run_chained(
        const std::vector<OnnxInput>& inputs, const char* output,
        IEngine& consumer, std::vector<OnnxInput>& consumer_inputs,
        const char* consumer_input, const char* consumer_output) {
        if (!output || !consumer_input || !consumer_output) {
            throw std::invalid_argument("Chained tensor name is missing");
        }
        for (auto& input : consumer_inputs) {
            if (input.name && std::string(input.name) == consumer_input) {
                input.floats = run(inputs, {output});
                size_t count = 1;
                for (const auto dimension : input.shape) {
                    if (dimension <= 0 || count > SIZE_MAX / static_cast<size_t>(dimension)) {
                        throw std::invalid_argument("Invalid chained tensor shape");
                    }
                    count *= static_cast<size_t>(dimension);
                }
                if (input.type != OnnxInput::Type::Float || input.floats.size() != count) {
                    throw std::invalid_argument("Chained tensor shape or type mismatch");
                }
                return consumer.run(consumer_inputs, {consumer_output});
            }
        }
        throw std::invalid_argument("Chained input tensor not found");
    }
    std::map<std::string, std::string> runtime_info() const {
        std::lock_guard<std::mutex> lock(runtime_mutex_);
        return runtime_info_;
    }
    void add_fallback_reason(const std::string& reason) {
        if (reason.empty()) return;
        std::lock_guard<std::mutex> lock(runtime_mutex_);
        auto& value = runtime_info_["fallback_reason"];
        value += (value.empty() ? "" : "; ") + reason;
    }
    // 引擎某输入的静态形状；动态/未知返回空（用于 TRT 固定形状校验）
    virtual std::vector<int64_t> input_shape(const std::string&) const { return {}; }
    virtual void unload() = 0;
protected:
    void record_runtime(const std::string& backend, const std::filesystem::path& asset, bool reset_reason = false) {
        std::lock_guard<std::mutex> lock(runtime_mutex_);
        runtime_info_["backend"] = backend;
        runtime_info_["asset"] = asset.string();
        if (reset_reason) runtime_info_["fallback_reason"].clear();
    }
private:
    mutable std::mutex runtime_mutex_;
    std::map<std::string, std::string> runtime_info_{{"backend", "unavailable"}, {"asset", ""}, {"fallback_reason", ""}};
};

// 工厂：model_path 为 .onnx；同目录 <stem>.engine 存在时优先 TensorRT，
// 否则回退 ONNX Runtime。返回已加载的引擎（失败返回 nullptr）。
std::unique_ptr<IEngine> make_engine(const std::filesystem::path& model_path);

class OnnxEngine final : public IEngine {
public:
    OnnxEngine() = default;

    bool load(const std::filesystem::path& model_path);
    bool loaded() const {
#ifdef USE_ONNX
        return session_ != nullptr;
#else
        return loaded_;
#endif
    }
    std::optional<OnnxInput::Type> input_type(const std::string& name) const;
    std::vector<int64_t> input_shape(const std::string& name) const override;

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
    // CUDA EP 运行期失败（如 provider 二进制与本机 sm 架构不符）时，
    // 重建纯 CPU 会话并重试一次
    bool reload_cpu();
    std::vector<float> run_once(
        const std::vector<const char*>& input_names,
        const std::vector<Ort::Value>& input_tensors,
        const std::vector<const char*>& output_names);

    std::unique_ptr<Ort::Env> env_;
    std::unique_ptr<Ort::SessionOptions> session_opts_;
    std::unique_ptr<Ort::Session> session_;
    Ort::MemoryInfo mem_info_{nullptr};
#else
    bool loaded_ = false;
#endif
    bool cuda_attached_ = false;
    std::filesystem::path model_path_;
    std::unordered_map<std::string, OnnxInput::Type> input_types_;
    std::unordered_map<std::string, std::vector<int64_t>> input_shapes_;
};

} // namespace rvc
