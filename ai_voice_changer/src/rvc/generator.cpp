// rvc/generator.cpp
#include "rvc/generator.hpp"
#include "utils/logging.hpp"

#include <algorithm>
#include <stdexcept>
#include <utility>

namespace mozart {

Generator::Generator(std::shared_ptr<InferEngine> engine,
                     const std::string& onnx_path,
                     int sample_rate, int spk_id, bool has_f0)
    : engine_(std::move(engine)),
      spk_id_(spk_id),
      has_f0_(has_f0),
      sample_rate_(sample_rate) {
    if (!engine_) throw std::runtime_error("Generator: null engine");
    session_ = engine_->create_session(onnx_path);
    if (!session_) throw std::runtime_error("Generator: session create failed");
    MOZART_INFO("Generator: session ready (%s) @ %dHz spk=%d f0=%d",
                onnx_path.c_str(), sample_rate_, spk_id_, has_f0_);
}

Generator::~Generator() = default;

bool Generator::run(std::span<const float> content_feats,
                    const std::vector<int64_t>& content_shape,
                    std::span<const float> f0,
                    std::span<const float> pitchf,
                    std::vector<float>& out_audio,
                    int64_t& out_samples) {
    if (!session_) return false;
    out_samples = 0;

    std::vector<std::pair<std::string, Tensor>> inputs;
    std::vector<std::pair<std::string, Tensor>> outputs;

    Tensor phone;
    phone.shape = content_shape;
    phone.data.assign(content_feats.begin(), content_feats.end());
    inputs.emplace_back("phone", std::move(phone));

    int64_t t = !content_shape.empty() ? content_shape[0] : 0;

    Tensor phone_len;
    phone_len.shape = {1};
    phone_len.data  = {static_cast<float>(t)};
    inputs.emplace_back("phone_lengths", std::move(phone_len));

    Tensor sid;
    sid.shape = {1};
    sid.data  = {static_cast<float>(spk_id_)};
    inputs.emplace_back("sid", std::move(sid));

    if (has_f0_) {
        Tensor pitch_t;
        pitch_t.shape.assign(content_shape.begin(),
                              content_shape.begin() +
                                  std::min<size_t>(content_shape.size(), 1));
        if (pitch_t.shape.empty()) pitch_t.shape = {1};
        pitch_t.data.assign(f0.begin(), f0.end());
        inputs.emplace_back("pitch", std::move(pitch_t));

        Tensor pitchf_t;
        pitchf_t.shape = content_shape.size() > 0
                              ? std::vector<int64_t>{content_shape[0]}
                              : std::vector<int64_t>{1};
        pitchf_t.data.assign(pitchf.begin(), pitchf.end());
        inputs.emplace_back("pitchf", std::move(pitchf_t));
    }

    outputs.emplace_back("audio", Tensor{});

    try {
        session_->run(inputs, outputs);
    } catch (const std::exception& e) {
        MOZART_WARN("Generator::run failed: %s", e.what());
        return false;
    }

    out_audio   = std::move(outputs[0].second.data);
    out_samples = out_audio.size();
    return true;
}

}  // namespace mozart