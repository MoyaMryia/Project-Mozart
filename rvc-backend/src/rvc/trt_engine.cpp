// trt_engine.cpp — TensorRT 10 直载实现（见头文件）
#include "rvc/trt_engine.hpp"
#include "rvc/profile.hpp"
#include <spdlog/spdlog.h>
#include <fstream>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <set>
#include <cstdlib>
#include <algorithm>

#ifdef USE_TENSORRT
#include <NvInfer.h>
#include <cuda_runtime.h>
#endif

namespace rvc {

#ifdef USE_TENSORRT

namespace {

class TrtLogger : public nvinfer1::ILogger {
public:
    void log(Severity severity, const char* msg) noexcept override {
        if (severity <= Severity::kWARNING) {
            spdlog::warn("[trt] {}", msg);
        }
    }
};

TrtLogger g_trt_logger;

size_t dtype_size(int32_t dtype) {
    switch (dtype) {
        case 0: return 4;   // kFLOAT
        case 3: return 4;   // kINT32
        case 8: return 8;   // kINT64
        default: return 4;
    }
}

std::vector<int64_t> to_dims(const nvinfer1::Dims& d) {
    return {d.d, d.d + d.nbDims};
}

nvinfer1::Dims from_dims(const std::vector<int64_t>& values) {
    if (values.size() > nvinfer1::Dims::MAX_DIMS) {
        throw std::invalid_argument("TRT input rank exceeds MAX_DIMS");
    }
    nvinfer1::Dims dims;
    dims.nbDims = static_cast<int32_t>(values.size());
    for (size_t i = 0; i < values.size(); ++i) {
        if (values[i] <= 0 || values[i] > std::numeric_limits<int32_t>::max()) {
            throw std::invalid_argument("TRT input dimension is invalid");
        }
        dims.d[i] = static_cast<int32_t>(values[i]);
    }
    return dims;
}

} // namespace

std::string TrtEngine::dtype_name(int32_t dtype) {
    switch (dtype) {
        case 0: return "float";
        case 3: return "int32";
        case 8: return "int64";
        default: return "dt" + std::to_string(dtype);
    }
}

bool TrtEngine::load(const std::filesystem::path& engine_path) {
    unload();
    if (!std::filesystem::exists(engine_path)) {
        spdlog::error("TRT engine not found: {}", engine_path.string());
        return false;
    }

    std::ifstream f(engine_path, std::ios::binary);
    std::string blob((std::istreambuf_iterator<char>(f)),
                     std::istreambuf_iterator<char>());
    if (blob.empty()) {
        spdlog::error("TRT engine empty: {}", engine_path.string());
        return false;
    }

    auto* runtime = nvinfer1::createInferRuntime(g_trt_logger);
    if (!runtime) {
        spdlog::error("TRT runtime creation failed");
        return false;
    }
    engine_ = runtime->deserializeCudaEngine(blob.data(), blob.size());
    delete runtime;
    if (!engine_) {
        spdlog::error("TRT deserialize failed: {}", engine_path.string());
        return false;
    }
    auto* engine = static_cast<nvinfer1::ICudaEngine*>(engine_);
    context_ = engine->createExecutionContext();
    if (!context_) {
        spdlog::error("TRT execution context creation failed");
        unload();
        return false;
    }
    if (cudaStreamCreateWithFlags(
            reinterpret_cast<cudaStream_t*>(&stream_), cudaStreamNonBlocking)
        != cudaSuccess) {
        spdlog::error("cudaStreamCreate failed");
        unload();
        return false;
    }

    // 枚举 I/O 张量。固定形状立即分配；动态形状在 run() 设置 profile
    // shape 后按实际大小延迟分配。
    tensors_.clear();
    input_types_.clear();
    for (int32_t i = 0; i < engine->getNbIOTensors(); ++i) {
        const char* name = engine->getIOTensorName(i);
        Tensor t;
        t.name = name;
        t.is_input = engine->getTensorIOMode(name) == nvinfer1::TensorIOMode::kINPUT;
        t.dtype = static_cast<int32_t>(engine->getTensorDataType(name));
        t.dims = to_dims(engine->getTensorShape(name));
        t.elems = 1;
        bool dynamic = false;
        for (auto d : t.dims) {
            if (d < 0) {
                dynamic = true;
                continue;
            }
            t.elems *= static_cast<size_t>(d);
        }
        if (dynamic) {
            t.elems = 0;
            t.bytes = 0;
        } else {
            t.bytes = t.elems * dtype_size(t.dtype);
            if (cudaMalloc(&t.dev, t.bytes) != cudaSuccess) {
                spdlog::error("cudaMalloc {} bytes failed for {}", t.bytes, name);
                unload();
                return false;
            }
            t.capacity_bytes = t.bytes;
        }
        if (t.is_input) {
            input_types_[t.name] = t.dtype == 8 ? OnnxInput::Type::Int64
                                                : OnnxInput::Type::Float;
        }
        tensors_.push_back(std::move(t));
    }

    std::string io_desc;
    for (const auto& t : tensors_) {
        io_desc += (t.is_input ? " in " : " out ") + t.name + "[";
        for (size_t k = 0; k < t.dims.size(); ++k) {
            io_desc += std::to_string(t.dims[k]);
            if (k + 1 < t.dims.size()) io_desc += "x";
        }
        io_desc += ":" + dtype_name(t.dtype) + "]";
    }
    spdlog::info("TRT engine loaded: {} ({} tensors){}", engine_path.filename().string(),
                 tensors_.size(), io_desc);
    record_runtime("tensorrt", engine_path, true);
    return true;
}

std::optional<OnnxInput::Type> TrtEngine::input_type(const std::string& name) const {
    const auto it = input_types_.find(name);
    return it == input_types_.end() ? std::nullopt
                                    : std::optional<OnnxInput::Type>(it->second);
}

std::vector<int64_t> TrtEngine::input_shape(const std::string& name) const {
    for (const auto& t : tensors_) {
        if (t.is_input && t.name == name) return t.dims;
    }
    return {};
}

namespace {

// 异常返回前完成排队工作，防止 CUDA 继续访问已释放的主机输入。
class StreamDrain {
public:
    explicit StreamDrain(void* stream) : stream_(static_cast<cudaStream_t>(stream)) {}
    ~StreamDrain() { if (stream_) cudaStreamSynchronize(stream_); }
    void release() { stream_ = nullptr; }
private:
    cudaStream_t stream_;
};

size_t checked_elements(const std::vector<int64_t>& shape) {
    size_t result = 1;
    for (const auto dimension : shape) {
        if (dimension <= 0 || result > SIZE_MAX / static_cast<size_t>(dimension)) {
            throw std::invalid_argument("TRT tensor shape is invalid or too large");
        }
        result *= static_cast<size_t>(dimension);
    }
    return result;
}

} // namespace

TrtEngine::Tensor& TrtEngine::enqueue(
    const std::vector<OnnxInput>& inputs, const char* output_name,
    void* execution_stream, const char* linked_name, const Tensor* linked) {
    if (!context_) throw std::runtime_error("TRT engine not loaded");
    if (!output_name) throw std::invalid_argument("TRT output name is missing");
    auto* context = static_cast<nvinfer1::IExecutionContext*>(context_);
    auto stream = static_cast<cudaStream_t>(execution_stream);
    auto find_tensor = [&](const char* name) -> Tensor* {
        if (!name) return nullptr;
        for (auto& tensor : tensors_) if (tensor.name == name) return &tensor;
        return nullptr;
    };
    Tensor* output = find_tensor(output_name);
    if (!output || output->is_input || output->dtype != 0) {
        throw std::invalid_argument("TRT requested output must be a float output tensor");
    }
    std::set<std::string> supplied;
    for (const auto& input : inputs) {
        Tensor* tensor = find_tensor(input.name);
        if (!tensor || !tensor->is_input || !supplied.insert(input.name).second) {
            throw std::invalid_argument("TRT input is missing, unknown, or duplicated");
        }
        checked_elements(input.shape);
        if (input.shape.size() != tensor->dims.size()) {
            throw std::invalid_argument("TRT input rank mismatch: " + tensor->name);
        }
        for (size_t axis = 0; axis < input.shape.size(); ++axis) {
            if (tensor->dims[axis] > 0 && tensor->dims[axis] != input.shape[axis]) {
                throw std::invalid_argument("TRT static input shape mismatch: " + tensor->name);
            }
        }
        if (!context->setInputShape(tensor->name.c_str(), from_dims(input.shape))) {
            throw std::invalid_argument("TRT profile rejected input shape: " + tensor->name);
        }
    }
    for (const auto& tensor : tensors_) {
        if (tensor.is_input && supplied.count(tensor.name) == 0) {
            throw std::invalid_argument("TRT required input is missing: " + tensor.name);
        }
    }
    bool linked_bound = false;
    for (auto& tensor : tensors_) {
        const auto concrete = to_dims(context->getTensorShape(tensor.name.c_str()));
        tensor.elems = checked_elements(concrete);
        const size_t item_bytes = dtype_size(tensor.dtype);
        if (tensor.elems > SIZE_MAX / item_bytes) {
            throw std::invalid_argument("TRT tensor byte size overflow");
        }
        tensor.bytes = tensor.elems * item_bytes;
        if (linked && linked_name && tensor.name == linked_name) {
            if (!tensor.is_input || tensor.dtype != linked->dtype || tensor.bytes != linked->bytes) {
                throw std::invalid_argument("TRT chained tensor type or shape mismatch");
            }
            const auto input = std::find_if(inputs.begin(), inputs.end(), [&](const auto& value) {
                return value.name && tensor.name == value.name;
            });
            if (input == inputs.end() || input->shape != linked->dims || input->type != OnnxInput::Type::Float) {
                throw std::invalid_argument("TRT chained tensor layout mismatch");
            }
            if (!context->setTensorAddress(tensor.name.c_str(), linked->dev)) {
                throw std::runtime_error("TRT failed to bind chained tensor");
            }
            linked_bound = true;
            continue;
        }
        if (tensor.bytes > tensor.capacity_bytes) {
            if (tensor.dev) cudaFree(tensor.dev);
            tensor.dev = nullptr;
            if (cudaMalloc(&tensor.dev, tensor.bytes) != cudaSuccess) {
                throw std::runtime_error("TRT tensor allocation failed: " + tensor.name);
            }
            tensor.capacity_bytes = tensor.bytes;
        }
        if (!context->setTensorAddress(tensor.name.c_str(), tensor.dev)) {
            throw std::runtime_error("TRT tensor binding failed: " + tensor.name);
        }
    }
    if (linked && !linked_bound) throw std::invalid_argument("TRT chained input not found");
    for (const auto& input : inputs) {
        Tensor* tensor = find_tensor(input.name);
        if (linked && linked_name && tensor->name == linked_name) continue;
        const void* host = nullptr;
        if (tensor->dtype == 8) {
            if (input.type != OnnxInput::Type::Int64 || input.int64s.size() != tensor->elems) {
                throw std::invalid_argument("TRT int64 input mismatch: " + tensor->name);
            }
            host = input.int64s.data();
        } else if (tensor->dtype == 3) {
            const size_t count = input.type == OnnxInput::Type::Int64 ? input.int64s.size() : input.floats.size();
            if (count != tensor->elems) throw std::invalid_argument("TRT int32 input mismatch: " + tensor->name);
            tensor->int32_staging.resize(count);
            for (size_t i = 0; i < count; ++i) {
                const double value = input.type == OnnxInput::Type::Int64
                    ? static_cast<double>(input.int64s[i]) : static_cast<double>(input.floats[i]);
                if (!(value >= INT32_MIN && value <= INT32_MAX)) {
                    throw std::invalid_argument("TRT int32 input is out of range: " + tensor->name);
                }
                tensor->int32_staging[i] = static_cast<int32_t>(value);
            }
            host = tensor->int32_staging.data();
        } else if (tensor->dtype == 0) {
            if (input.type != OnnxInput::Type::Float || input.floats.size() != tensor->elems) {
                throw std::invalid_argument("TRT float input mismatch: " + tensor->name);
            }
            host = input.floats.data();
        } else {
            throw std::invalid_argument("TRT input dtype is unsupported: " + tensor->name);
        }
        if (cudaMemcpyAsync(tensor->dev, host, tensor->bytes, cudaMemcpyHostToDevice, stream) != cudaSuccess) {
            throw std::runtime_error("TRT input copy failed: " + tensor->name);
        }
    }
    if (!context->enqueueV3(stream)) throw std::runtime_error("TRT enqueueV3 failed");
    return *output;
}

std::vector<float> TrtEngine::read_output(const Tensor& output, void* execution_stream) {
    auto stream = static_cast<cudaStream_t>(execution_stream);
    std::vector<float> result(output.elems);
    // 局部输出缓冲区须比排队的 D2H 工作存活更久。
    StreamDrain drain(execution_stream);
    if (cudaMemcpyAsync(result.data(), output.dev, output.bytes, cudaMemcpyDeviceToHost, stream) != cudaSuccess
        || cudaStreamSynchronize(stream) != cudaSuccess) {
        throw std::runtime_error("TRT output copy or synchronization failed");
    }
    drain.release();
    return result;
}

std::vector<float> TrtEngine::run(const std::vector<OnnxInput>& inputs,
                                  const std::vector<const char*>& output_names) {
    if (output_names.size() != 1) throw std::invalid_argument("TRT requires one named output");
    std::lock_guard<std::mutex> lock(execution_mutex_);
    StreamDrain drain(stream_);
    const auto start = profile::Clock::now();
    auto& output = enqueue(inputs, output_names.front(), stream_);
    const auto queued = profile::Clock::now();
    auto result = read_output(output, stream_);
    drain.release();
    if (profile::enabled()) {
        const auto done = profile::Clock::now();
        spdlog::info("[profile][trt:{}] prepare_enqueue={:.3f}ms output_wait={:.3f}ms total={:.3f}ms",
            output_names.front(), profile::elapsed_ms(start, queued),
            profile::elapsed_ms(queued, done), profile::elapsed_ms(start, done));
    }
    return result;
}

std::vector<float> TrtEngine::run_chained(
    const std::vector<OnnxInput>& inputs, const char* output_name,
    IEngine& consumer, std::vector<OnnxInput>& consumer_inputs,
    const char* consumer_input, const char* consumer_output) {
    auto* next = dynamic_cast<TrtEngine*>(&consumer);
    const char* setting = std::getenv("MOZART_RVC_DEVICE_CHAIN");
    if (!next || next == this || (setting && std::string(setting) == "0")) {
        return IEngine::run_chained(inputs, output_name, consumer, consumer_inputs, consumer_input, consumer_output);
    }
    std::scoped_lock lock(execution_mutex_, next->execution_mutex_);
    StreamDrain drain(stream_);
    const auto start = profile::Clock::now();
    auto& intermediate = enqueue(inputs, output_name, stream_);
    // 固定实时契约使用完整 z。动态输出保存本次实际形状。
    Tensor view = {};
    view.dtype = intermediate.dtype;
    view.bytes = intermediate.bytes;
    view.dev = intermediate.dev;
    view.dims = to_dims(static_cast<nvinfer1::IExecutionContext*>(context_)->getTensorShape(output_name));
    auto& output = next->enqueue(consumer_inputs, consumer_output, stream_, consumer_input, &view);
    auto result = next->read_output(output, stream_);
    drain.release();
    if (profile::enabled()) {
        spdlog::info("[profile][trt-chain] {}->{} total={:.3f}ms", output_name, consumer_input,
            profile::elapsed_ms(start, profile::Clock::now()));
    }
    return result;
}

std::vector<float> TrtEngine::run(
    const std::vector<const char*>& input_names,
    const std::vector<std::vector<int64_t>>& input_shapes,
    const std::vector<std::vector<float>>& input_data,
    const std::vector<const char*>& output_names) {
    std::vector<OnnxInput> inputs;
    inputs.reserve(input_names.size());
    for (size_t i = 0; i < input_names.size(); ++i) {
        inputs.push_back({input_names[i], input_shapes[i], OnnxInput::Type::Float,
                          input_data[i], {}});
    }
    return run(inputs, output_names);
}

void TrtEngine::unload() {
    if (stream_) cudaStreamDestroy(static_cast<cudaStream_t>(stream_));
    for (auto& t : tensors_) {
        if (t.dev) cudaFree(t.dev);
    }
    tensors_.clear();
    if (context_) delete static_cast<nvinfer1::IExecutionContext*>(context_);
    if (engine_) delete static_cast<nvinfer1::ICudaEngine*>(engine_);
    context_ = nullptr;
    engine_ = nullptr;
    stream_ = nullptr;
    input_types_.clear();
    record_runtime("unavailable", {}, true);
}

#else // !USE_TENSORRT — 空实现（头文件桩已够，这里无需额外代码）

#endif

} // namespace rvc
