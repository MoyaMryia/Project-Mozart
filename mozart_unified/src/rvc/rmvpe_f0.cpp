#include <mozart/rvc/rmvpe_f0.hpp>
#include <mozart/utils/logging.hpp>
#include <cmath>

namespace mozart {

struct RMVPEF0::Impl {
    std::unique_ptr<InferSession> session;
    Method method;
};

RMVPEF0::RMVPEF0(const std::string& model_path,
                 std::shared_ptr<InferEngine> engine,
                 Method method)
    : impl_(std::make_unique<Impl>()) {
    impl_->method = method;
    if (method == Method::RMVPE) {
        impl_->session = engine->create_session(model_path);
        MOZART_LOG_INFO("RMVPE F0 extractor loaded from {}", model_path);
    } else {
        MOZART_LOG_INFO("Using {} F0 extraction method",
                       method == Method::HARVEST ? "Harvest" : "PM");
    }
}

RMVPEF0::~RMVPEF0() = default;

std::vector<float> RMVPEF0::extract(const std::vector<float>& audio,
                                    size_t sample_rate,
                                    int pitch_shift) {
    // TODO: Implement actual F0 extraction
    // For now, return zero F0 (unvoiced)
    size_t hop = 160;  // 10ms hop for 16kHz
    size_t T = audio.size() / hop;
    std::vector<float> f0(T, 0.0f);

    // Apply pitch shift if needed
    if (pitch_shift != 0) {
        double factor = std::pow(2.0, pitch_shift / 12.0);
        for (auto& v : f0) {
            if (v > 0) v *= factor;
        }
    }

    return f0;
}

void RMVPEF0::median_filter(std::vector<float>& f0, int radius) {
    if (radius <= 0 || f0.empty()) return;

    std::vector<float> filtered(f0.size());
    for (size_t i = 0; i < f0.size(); ++i) {
        std::vector<float> window;
        for (int j = -radius; j <= radius; ++j) {
            int idx = static_cast<int>(i) + j;
            if (idx >= 0 && idx < static_cast<int>(f0.size())) {
                window.push_back(f0[idx]);
            }
        }
        std::sort(window.begin(), window.end());
        filtered[i] = window[window.size() / 2];
    }
    f0 = std::move(filtered);
}

}  // namespace mozart
