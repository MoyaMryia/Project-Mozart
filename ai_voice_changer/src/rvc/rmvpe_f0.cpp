// rvc/rmvpe_f0.cpp
#include "rvc/rmvpe_f0.hpp"
#include "utils/logging.hpp"

#include <stdexcept>

namespace mozart {

RmvpeF0::RmvpeF0(std::shared_ptr<InferEngine> engine,
                 const std::string& onnx_path)
    : engine_(std::move(engine)) {
    if (!engine_) throw std::runtime_error("RmvpeF0: null engine");
    session_ = engine_->create_session(onnx_path);
    if (!session_) throw std::runtime_error("RmvpeF0: session create failed");
    MOZART_INFO("RmvpeF0: session ready (%s)", onnx_path.c_str());
}

RmvpeF0::~RmvpeF0() = default;

bool RmvpeF0::extract(std::span<const float> pcm16k,
                      std::vector<float>& out_f0) {
    if (!session_) return false;

    Tensor in_tensor;
    in_tensor.shape = {1, static_cast<int64_t>(pcm16k.size())};
    in_tensor.data.assign(pcm16k.begin(), pcm16k.end());

    std::vector<std::pair<std::string, Tensor>> inputs{{"audio", std::move(in_tensor)}};
    std::vector<std::pair<std::string, Tensor>> outputs{{"f0", {}}};

    try {
        session_->run(inputs, outputs);
    } catch (const std::exception& e) {
        MOZART_WARN("RmvpeF0::run failed: %s", e.what());
        return false;
    }

    out_f0 = std::move(outputs[0].second.data);
    return true;
}

}  // namespace mozart