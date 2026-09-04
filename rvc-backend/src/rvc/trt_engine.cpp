// trt_engine.cpp — TensorRT 10 直载实现（见头文件）
#include "rvc/trt_engine.hpp"
#include <spdlog/spdlog.h>
#include <fstream>
#include <cstring>
#include <limits>
#include <stdexcept>

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

std::vector<float> TrtEngine::run(const std::vector<OnnxInput>& inputs,
                                  const std::vector<const char*>& output_names) {
    if (!context_) throw std::runtime_error("TRT engine not loaded");
    auto* context = static_cast<nvinfer1::IExecutionContext*>(context_);
    auto* stream = static_cast<cudaStream_t>(stream_);

    auto find_tensor = [&](const std::string& name) -> Tensor* {
        for (auto& t : tensors_) {
            if (t.name == name) return &t;
        }
        return nullptr;
    };

    // Set every runtime input shape before asking TensorRT for output shapes.
    for (const auto& input : inputs) {
        Tensor* t = find_tensor(input.name);
        if (!t || !t->is_input) {
            throw std::runtime_error("TRT input tensor not found: " + std::string(input.name));
        }
        if (input.shape.size() != t->dims.size()) {
            throw std::invalid_argument("TRT input rank mismatch: " + std::string(input.name));
        }
        for (size_t dimension = 0; dimension < input.shape.size(); ++dimension) {
            if (t->dims[dimension] > 0
                && t->dims[dimension] != input.shape[dimension]) {
                throw std::invalid_argument("TRT static input shape mismatch: " + std::string(input.name));
            }
        }
        if (!context->setInputShape(t->name.c_str(), from_dims(input.shape))) {
            throw std::invalid_argument("TRT profile rejected input shape: " + std::string(input.name));
        }
    }

    for (auto& tensor : tensors_) {
        const auto concrete = context->getTensorShape(tensor.name.c_str());
        tensor.elems = 1;
        for (int32_t dimension = 0; dimension < concrete.nbDims; ++dimension) {
            if (concrete.d[dimension] <= 0) {
                throw std::runtime_error("TRT could not resolve shape for " + tensor.name);
            }
            tensor.elems *= static_cast<size_t>(concrete.d[dimension]);
        }
        tensor.bytes = tensor.elems * dtype_size(tensor.dtype);
        if (tensor.bytes > tensor.capacity_bytes) {
            if (tensor.dev) cudaFree(tensor.dev);
            tensor.dev = nullptr;
            if (cudaMalloc(&tensor.dev, tensor.bytes) != cudaSuccess) {
                throw std::runtime_error(
                    "cudaMalloc " + std::to_string(tensor.bytes)
                    + " bytes failed for " + tensor.name);
            }
            tensor.capacity_bytes = tensor.bytes;
        }
        context->setTensorAddress(tensor.name.c_str(), tensor.dev);
    }

    // H2D：按引擎实际 dtype 拷贝（int64 输入若引擎是 int32 则降转换）
    for (const auto& input : inputs) {
        Tensor* t = find_tensor(input.name);
        const void* host = nullptr;
        size_t bytes = 0;
        std::vector<int32_t> as_int32;
        if (t->dtype == 8) {                       // kINT64
            if (input.int64s.size() != t->elems) throw std::invalid_argument("TRT int64 shape mismatch: " + std::string(input.name));
            host = input.int64s.data();
            bytes = t->bytes;
        } else if (t->dtype == 3) {                // kINT32（ONNX int64 输入的常见降级）
            as_int32.resize(t->elems);
            const size_t src = input.type == OnnxInput::Type::Int64
                ? input.int64s.size() : input.floats.size();
            if (src != t->elems) throw std::invalid_argument("TRT int32 shape mismatch: " + std::string(input.name));
            for (size_t k = 0; k < t->elems; ++k) {
                const int64_t v = input.type == OnnxInput::Type::Int64
                    ? input.int64s[k] : static_cast<int64_t>(input.floats[k]);
                as_int32[k] = static_cast<int32_t>(v);
            }
            host = as_int32.data();
            bytes = t->bytes;
        } else {                                   // kFLOAT
            if (input.floats.size() != t->elems) throw std::invalid_argument("TRT float shape mismatch: " + std::string(input.name));
            host = input.floats.data();
            bytes = t->bytes;
        }
        if (cudaMemcpyAsync(t->dev, host, bytes, cudaMemcpyHostToDevice, stream)
            != cudaSuccess) {
            throw std::runtime_error("cudaMemcpyAsync H2D failed for " + std::string(input.name));
        }
    }

    if (!context->enqueueV3(stream)) {
        throw std::runtime_error("enqueueV3 failed");
    }

    // The D2H copy is ordered after inference on the same stream, so a separate
    // pre-copy synchronization only adds a host round trip.
    (void)output_names;
    for (const auto& t : tensors_) {
        if (t.is_input) continue;
        std::vector<float> out(t.elems);
        if (cudaMemcpyAsync(out.data(), t.dev, t.elems * sizeof(float),
                            cudaMemcpyDeviceToHost, stream) != cudaSuccess) {
            throw std::runtime_error("cudaMemcpyAsync D2H failed for " + t.name);
        }
        if (cudaStreamSynchronize(stream) != cudaSuccess) {
            throw std::runtime_error("cudaStreamSynchronize failed for " + t.name);
        }
        return out;
    }
    throw std::runtime_error("TRT engine has no output tensor");
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
}

#else // !USE_TENSORRT — 空实现（头文件桩已够，这里无需额外代码）

#endif

} // namespace rvc
