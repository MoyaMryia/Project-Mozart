#include "rvc/feature_extractor.hpp"
#include "rvc/trt_engine.hpp"

#include <spdlog/spdlog.h>
#include <fstream>
#include <cmath>
#include <complex>
#include <numbers>
#include <limits>

namespace rvc {

FeatureExtractor::FeatureExtractor(
    const std::filesystem::path& hubert_path,
    const std::optional<std::filesystem::path>& rmvpe_path,
    const std::string& device,
    bool half,
    bool mock_hubert,
    bool mock_rmvpe
)
    : hubert_path_(hubert_path)
    , rmvpe_path_(rmvpe_path)
    , device_(device)
    , half_(half)
    , mock_hubert_(mock_hubert)
    , mock_rmvpe_(mock_rmvpe)
{
    if (std::filesystem::exists(hubert_path_)) {
        hubert_engine_ = make_engine(hubert_path_);
        // TRT 引擎是固定形状：HuBERT 必须接受我们的整窗长度（流式 2s=32000）
        if (auto* trt = dynamic_cast<TrtEngine*>(hubert_engine_.get())) {
            const auto shape = trt->input_shape("audio");
            const int64_t need = 32000; // 流式窗口 2s @16k
            if (!shape.empty() && (shape.size() != 2 || shape[1] != need)) {
                spdlog::warn("HuBERT TRT 引擎形状不符（audio={}[{}] ≠ [1,{}]），回退 ONNX",
                             shape.size() >= 2 ? shape[1] : -1, shape.size(), need);
                hubert_engine_.reset();
            }
        }
        if (hubert_engine_ && hubert_engine_->loaded()) {
            spdlog::info("HuBERT engine loaded: {}", hubert_path_.string());
        }
    } else {
        spdlog::warn("HuBERT model not found at {}", hubert_path_.string());
    }

    if (rmvpe_path_ && std::filesystem::exists(*rmvpe_path_)) {
        rmvpe_engine_ = make_engine(*rmvpe_path_);
        // RMVPE 固定 [1,128,128]（extract_f0 会零填充到该形状）
        if (auto* trt = dynamic_cast<TrtEngine*>(rmvpe_engine_.get())) {
            const auto shape = trt->input_shape("mel");
            if (!shape.empty() && shape != std::vector<int64_t>({1, 128, 128})) {
                spdlog::warn("RMVPE TRT 引擎形状不符，回退 ONNX");
                rmvpe_engine_.reset();
            }
        }
        if (rmvpe_engine_ && rmvpe_engine_->loaded()) {
            spdlog::info("RMVPE engine loaded: {}", rmvpe_path_->string());
        }
    } else if (rmvpe_path_) {
        spdlog::warn("RMVPE path not found: {}", rmvpe_path_->string());
    }
}

FeatureExtractor::~FeatureExtractor() = default;

// ---- 真 mel 谱支持（匹配 RVC infer/rmvpe.py MelSpectrogram 前端）----
namespace {

// 迭代式 radix-2 复数 FFT（n_fft=1024，2 的幂）
void fft_radix2(std::vector<float>& re, std::vector<float>& im) {
    const size_t n = re.size();
    for (size_t i = 1, j = 0; i < n; ++i) {
        size_t bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) { std::swap(re[i], re[j]); std::swap(im[i], im[j]); }
    }
    for (size_t len = 2; len <= n; len <<= 1) {
        const float ang = -2.0f * static_cast<float>(M_PI) / static_cast<float>(len);
        const float w_re = std::cos(ang), w_im = std::sin(ang);
        for (size_t i = 0; i < n; i += len) {
            float cur_re = 1.0f, cur_im = 0.0f;
            for (size_t k = 0; k < len / 2; ++k) {
                const float u_re = re[i + k], u_im = im[i + k];
                const float v_re = re[i + k + len / 2] * cur_re - im[i + k + len / 2] * cur_im;
                const float v_im = re[i + k + len / 2] * cur_im + im[i + k + len / 2] * cur_re;
                re[i + k] = u_re + v_re;
                im[i + k] = u_im + v_im;
                re[i + k + len / 2] = u_re - v_re;
                im[i + k + len / 2] = u_im - v_im;
                const float n_re = cur_re * w_re - cur_im * w_im;
                cur_im = cur_re * w_im + cur_im * w_re;
                cur_re = n_re;
            }
        }
    }
}

float htk_hz_from_mel(float mel) {
    return 700.0f * (std::pow(10.0f, mel / 2595.0f) - 1.0f);
}

// HTK mel 滤波器组 [n_mels, n_fft/2+1]（librosa.filters.mel(..., htk=True) 等价）。
// librosa 的默认 Slaney 归一化必须保留，否则 RMVPE 的输入分布会改变。
std::vector<float> build_htk_mel_basis(uint32_t sr, int n_mels, int n_fft,
                                       float fmin, float fmax) {
    const int n_bins = n_fft / 2 + 1;
    std::vector<float> basis(static_cast<size_t>(n_mels) * n_bins, 0.0f);
    const float mel_min = 2595.0f * std::log10(1.0f + fmin / 700.0f);
    const float mel_max = 2595.0f * std::log10(1.0f + fmax / 700.0f);
    // n_mels + 2 个边界点
    std::vector<float> pts(static_cast<size_t>(n_mels) + 2);
    for (int m = 0; m < n_mels + 2; ++m) {
        pts[m] = htk_hz_from_mel(mel_min + (mel_max - mel_min) * m / (n_mels + 1));
    }
    for (int m = 0; m < n_mels; ++m) {
        const float lo = pts[m], center = pts[m + 1], hi = pts[m + 2];
        for (int k = 0; k < n_bins; ++k) {
            const float fk = static_cast<float>(k) * static_cast<float>(sr) / static_cast<float>(n_fft);
            float w = 0.0f;
            if (fk >= lo && fk <= hi) {
                if (fk <= center) {
                    w = center > lo ? (fk - lo) / (center - lo) : (fk == lo ? 1.0f : 0.0f);
                } else {
                    w = hi > center ? (hi - fk) / (hi - center) : (fk == hi ? 1.0f : 0.0f);
                }
            }
            const float slaney_norm = hi > lo ? 2.0f / (hi - lo) : 0.0f;
            basis[static_cast<size_t>(m) * n_bins + k] = w * slaney_norm;
        }
    }
    return basis;
}

} // namespace

std::vector<float> FeatureExtractor::compute_mel(
    const std::vector<float>& audio,
    uint32_t sample_rate,
    int n_mels, int n_fft, int hop_length
) {
    // RMVPE 前端规格：16kHz、hann(周期)、center=True(reflect)、n_fft=1024、hop=160、
    // 幅度谱 -> HTK mel(128, 30..8000Hz, Slaney norm) -> ln(clamp(x, 1e-5))
    const size_t n_samples = audio.size();
    if (n_samples == 0) return std::vector<float>(static_cast<size_t>(n_mels), 0.0f);
    const size_t pad = static_cast<size_t>(n_fft) / 2;
    const size_t frames = 1 + n_samples / static_cast<size_t>(hop_length);

    // 反射填充（torch.stft center=True 的 pad_mode 默认 reflect）
    std::vector<float> padded(n_samples + 2 * pad, 0.0f);
    for (size_t i = 0; i < pad; ++i) {
        const size_t li = pad - i;
        padded[i] = audio[li < n_samples ? li : n_samples - 1];
        const size_t ri = n_samples - 2 - i;
        padded[pad + n_samples + i] = audio[ri < n_samples ? ri : 0];
    }
    std::copy_n(audio.begin(), n_samples, padded.begin() + pad);

    // hann 周期窗 + mel 滤波器组缓存
    static std::vector<float> window;
    static std::vector<float> mel_basis;
    static int cached_fft = 0, cached_mels = 0;
    static uint32_t cached_sr = 0;
    if (cached_fft != n_fft || cached_mels != n_mels || cached_sr != sample_rate) {
        window.assign(n_fft, 0.0f);
        for (int i = 0; i < n_fft; ++i) {
            window[i] = 0.5f * (1.0f - std::cos(2.0f * static_cast<float>(M_PI) * i / n_fft));
        }
        mel_basis = build_htk_mel_basis(sample_rate, n_mels, n_fft,
                                        30.0f, std::min(8000.0f, sample_rate / 2.0f));
        cached_fft = n_fft; cached_mels = n_mels; cached_sr = sample_rate;
    }

    const int n_bins = n_fft / 2 + 1;
    std::vector<float> mel(static_cast<size_t>(frames) * n_mels, 0.0f);
    std::vector<float> re(n_fft), im(n_fft);
    for (size_t f = 0; f < frames; ++f) {
        const size_t off = f * static_cast<size_t>(hop_length);
        for (int i = 0; i < n_fft; ++i) {
            re[i] = padded[off + i] * window[i];
            im[i] = 0.0f;
        }
        fft_radix2(re, im);
        for (int k = 0; k < n_bins; ++k) {
            const float mag = std::sqrt(re[k] * re[k] + im[k] * im[k]);
            for (int m = 0; m < n_mels; ++m) {
                const float w = mel_basis[static_cast<size_t>(m) * n_bins + k];
                if (w != 0.0f) mel[f * n_mels + m] += w * mag;
            }
        }
        for (int m = 0; m < n_mels; ++m) {
            float& v = mel[f * n_mels + m];
            v = std::log(std::max(v, 1e-5f));
        }
    }
    return mel;
}

std::vector<float> FeatureExtractor::extract_f0(
    const std::vector<float>& audio,
    uint32_t sample_rate,
    const std::string& method
) {
    if (method == "rmvpe" && rmvpe_engine_ && rmvpe_engine_->loaded()) {
        auto mel = compute_mel(audio, sample_rate);
        // export_rmvpe_onnx.py exports the model with a fixed
        // [batch, mel_bin, time] tensor.
        // Run fixed-size windows so a 2s input does not lose its last ~0.7s.
        constexpr size_t rmvpe_frames = 128;
        constexpr size_t mel_bins = 128;
        const size_t mel_frames = mel.size() / mel_bins;
        std::vector<float> hz;
        hz.reserve(mel_frames);
        constexpr size_t pitch_bins = 360;
        constexpr float cents_origin = 1997.3794084376191f;
        constexpr float salience_threshold = 0.03f;
        const float log_silence = std::log(1e-5f);

        for (size_t frame_start = 0; frame_start < mel_frames; frame_start += rmvpe_frames) {
            std::vector<float> fixed_mel(rmvpe_frames * mel_bins, log_silence);
            const size_t copy_frames = std::min(rmvpe_frames, mel_frames - frame_start);
            for (size_t b = 0; b < mel_bins; ++b) {
                for (size_t t = 0; t < copy_frames; ++t) {
                    fixed_mel[b * rmvpe_frames + t] = mel[(frame_start + t) * mel_bins + b];
                }
            }

            const std::vector<int64_t> mel_shape = {
                1, static_cast<int64_t>(rmvpe_frames), static_cast<int64_t>(mel_bins)
            };
            const auto salience = rmvpe_engine_->run(
                {"mel"}, {mel_shape}, {fixed_mel}, {"f0"}
            );
            if (salience.size() < rmvpe_frames * pitch_bins) {
                throw std::runtime_error("RMVPE output shape is smaller than [128, 360]");
            }

            for (size_t frame = 0; frame < copy_frames; ++frame) {
                const auto begin = salience.begin() + static_cast<std::ptrdiff_t>(frame * pitch_bins);
                const auto maximum = std::max_element(
                    begin, begin + static_cast<std::ptrdiff_t>(pitch_bins));
                const size_t center = static_cast<size_t>(maximum - begin);
                float weight_sum = 0.0f;
                float cents_sum = 0.0f;
                for (int offset = -4; offset <= 4; ++offset) {
                    const int bin = static_cast<int>(center) + offset;
                    if (bin < 0 || bin >= static_cast<int>(pitch_bins)) continue;
                    const float weight = begin[bin];
                    weight_sum += weight;
                    cents_sum += weight * (cents_origin + 20.0f * static_cast<float>(bin));
                }
                const float max_salience = *maximum;
                if (max_salience <= salience_threshold || weight_sum <= std::numeric_limits<float>::epsilon()) {
                    hz.push_back(0.0f);
                } else {
                    hz.push_back(10.0f * std::pow(2.0f, (cents_sum / weight_sum) / 1200.0f));
                }
            }
        }
        spdlog::debug("RMVPE F0: {} frames extracted", hz.size());
        return hz;
    }

    if (method == "harvest") {
        return f0_harvest(audio, sample_rate);
    }

    if (method == "pm") {
        return f0_pm(audio, sample_rate);
    }

    if (!mock_rmvpe_) {
        throw std::runtime_error("RMVPE engine is unavailable and rvc.mock.rmvpe is false");
    }
    spdlog::warn("RMVPE mock enabled; returning zero F0");
    size_t n_frames = audio.size() / 512;
    if (n_frames == 0) n_frames = 1;
    return std::vector<float>(n_frames, 0.0f);
}

std::vector<float> FeatureExtractor::extract_features(
    const std::vector<float>& audio,
    uint32_t sample_rate
) {
    if (hubert_engine_ && hubert_engine_->loaded()) {
        size_t n_samples = audio.size();
        std::vector<int64_t> audio_shape = {1, static_cast<int64_t>(n_samples)};
        auto feats = hubert_engine_->run(
            {"audio"}, {audio_shape}, {audio}, {"features"}
        );
        spdlog::debug("HuBERT features: {} elements extracted", feats.size());
        return feats;
    }

    if (!mock_hubert_) {
        throw std::runtime_error("HuBERT engine is unavailable and rvc.mock.hubert is false");
    }
    spdlog::warn("HuBERT mock enabled; returning dummy features");
    size_t n_frames = audio.size() / 512;
    if (n_frames == 0) n_frames = 1;
    return std::vector<float>(n_frames * 768, 0.0f);
}

std::vector<float> FeatureExtractor::f0_harvest(
    const std::vector<float>& audio,
    uint32_t sample_rate
) {
    spdlog::warn("pyworld harvest not available; using zero F0");
    size_t n_frames = audio.size() / 512;
    if (n_frames == 0) n_frames = 1;
    return std::vector<float>(n_frames, 0.0f);
}

std::vector<float> FeatureExtractor::f0_pm(
    const std::vector<float>& audio,
    uint32_t sample_rate
) {
    spdlog::warn("parselmouth pm not available; using zero F0");
    size_t n_frames = audio.size() / 512;
    if (n_frames == 0) n_frames = 1;
    return std::vector<float>(n_frames, 0.0f);
}

} // namespace rvc
