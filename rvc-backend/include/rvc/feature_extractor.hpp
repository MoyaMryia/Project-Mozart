#pragma once

#include <cstdint>
#include <vector>
#include <memory>
#include <string>
#include <filesystem>
#include <optional>

#include "rvc/onnx_engine.hpp"

namespace rvc {

class FeatureExtractor {
public:
    FeatureExtractor(
        const std::filesystem::path& hubert_path,
        const std::optional<std::filesystem::path>& rmvpe_path,
        const std::string& device = "cuda",
        bool half = false,
        bool mock_hubert = true,
        bool mock_rmvpe = true,
        const std::optional<std::filesystem::path>& realtime_hubert_path = std::nullopt,
        const std::optional<std::filesystem::path>& realtime_rmvpe_path = std::nullopt
    );

    ~FeatureExtractor();

    std::vector<float> extract_f0(
        const std::vector<float>& audio,
        uint32_t sample_rate = 16000,
        const std::string& method = "rmvpe"
    );

    std::vector<float> extract_features(
        const std::vector<float>& audio,
        uint32_t sample_rate = 16000
    );

    std::vector<float> extract_f0_realtime(
        const std::vector<float>& audio,
        uint32_t sample_rate = 16000,
        const std::string& method = "rmvpe"
    );

    std::vector<float> extract_features_realtime(
        const std::vector<float>& audio,
        uint32_t sample_rate = 16000
    );

    // Diagnostic access to the RMVPE front-end mel (time-major [frames, 128]).
    std::vector<float> extract_mel(
        const std::vector<float>& audio,
        uint32_t sample_rate = 16000
    );

    bool is_hubert_loaded() const { return hubert_engine_ && hubert_engine_->loaded(); }
    bool is_rmvpe_loaded() const { return rmvpe_engine_ && rmvpe_engine_->loaded(); }
    bool supports_realtime_hubert_samples(size_t samples) const;
    bool supports_realtime_rmvpe_frames(size_t frames) const;

private:
    std::filesystem::path hubert_path_;
    std::optional<std::filesystem::path> rmvpe_path_;
    std::optional<std::filesystem::path> realtime_hubert_path_;
    std::optional<std::filesystem::path> realtime_rmvpe_path_;
    std::string device_;
    bool half_;
    bool mock_hubert_;
    bool mock_rmvpe_;

    std::unique_ptr<IEngine> hubert_engine_;
    std::unique_ptr<IEngine> rmvpe_engine_;
    std::unique_ptr<IEngine> realtime_hubert_engine_;
    std::unique_ptr<IEngine> realtime_rmvpe_engine_;
    // 固定形状 TRT 引擎的动态 ONNX 兜底（file 模式全长输入）
    std::unique_ptr<IEngine> hubert_onnx_engine_;
    std::unique_ptr<IEngine> rmvpe_onnx_engine_;

    std::vector<float> f0_harvest(const std::vector<float>& audio, uint32_t sample_rate);
    std::vector<float> f0_pm(const std::vector<float>& audio, uint32_t sample_rate);
    std::vector<float> extract_f0_impl(
        const std::vector<float>& audio,
        uint32_t sample_rate,
        const std::string& method,
        bool realtime
    );
    std::vector<float> extract_features_impl(
        const std::vector<float>& audio,
        uint32_t sample_rate,
        bool realtime
    );

    std::vector<float> compute_mel(const std::vector<float>& audio,
                                    uint32_t sample_rate,
                                    int n_mels = 128, int n_fft = 1024,
                                    int hop_length = 160);
};

} // namespace rvc
