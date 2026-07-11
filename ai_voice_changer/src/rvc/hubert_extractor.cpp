// rvc/hubert_extractor.cpp
#include "rvc/hubert_extractor.hpp"
#include "utils/logging.hpp"

#include <stdexcept>

namespace mozart {

HubertExtractor::HubertExtractor(std::shared_ptr<InferEngine> engine,
                                 const std::string& onnx_path)
    : engine_(std::move(engine)) {
    if (!engine_) throw std::runtime_error("HubertExtractor: null engine");
    session_ = engine_->create_session(onnx_path);
    if (!session_) throw std::runtime_error("HubertExtractor: session create failed");
    MOZART_INFO("HubertExtractor: session ready (%s)", onnx_path.c_str());
}

HubertExtractor::~HubertExtractor() = default;

bool HubertExtractor::extract(std::span<const float> pcm16k,
                               std::vector<float>& out_feats,
                               std::vector<int64_t>& out_shape) {
    if (!session_) return false;

    // HuBERT input convention: shape [1, T] float.
    Tensor in_tensor;
    in_tensor.shape = {1, static_cast<int64_t>(pcm16k.size())};
    in_tensor.data.assign(pcm16k.begin(), pcm16k.end());

    Tensor out_tensor;
    out_shape.clear();

    std::vector<std::pair<std::string, Tensor>> inputs{{in_name_, std::move(in_tensor)}};
    std::vector<std::pair<std::string, Tensor>> outputs{{out_names_[0], Tensor{}}};

    try {
        session_->run(inputs, outputs);
    } catch (const std::exception& e) {
        MOZART_WARN("HubertExtractor::run failed: %s", e.what());
        return false;
    }

    out_feats  = std::move(outputs[0].second.data);
    out_shape  = std::move(outputs[0].second.shape);
    return true;
}

}  // namespace mozart