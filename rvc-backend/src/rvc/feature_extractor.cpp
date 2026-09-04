#include "rvc/feature_extractor.hpp"
#include "rvc/trt_engine.hpp"

#include <spdlog/spdlog.h>
#include <fstream>
#include <cmath>
#include <complex>
#include <numbers>
#include <limits>
#include <thread>
#include <vector>

namespace rvc {

namespace {
IEngine* ensure_onnx_fallback(std::unique_ptr<IEngine>& slot,
                              const std::filesystem::path& onnx_path);
void warmup_zeros(IEngine& engine, const char* input,
                  std::vector<int64_t> shape, const char* output);
} // namespace

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
        // HuBERT accepts either a fixed positive sample length or a dynamic
        // optimization-profile axis. Other fixed lengths use ONNX fallback.
        if (auto* trt = dynamic_cast<TrtEngine*>(hubert_engine_.get())) {
            const auto shape = trt->input_shape("audio");
            const bool ok = shape.empty()
                || (shape.size() == 2 && shape[0] == 1 && shape[1] != 0);
            if (!ok) {
                spdlog::warn("HuBERT TRT 引擎形状不符（期望 [1,N]，实际 rank={}），回退 ONNX",
                             shape.size());
                hubert_engine_.reset();
            }
        }
        if (hubert_engine_ && hubert_engine_->loaded()) {
            const auto shape = hubert_engine_->input_shape("audio");
            if (shape.size() == 2 && shape[1] > 0) {
                warmup_zeros(
                    *hubert_engine_, "audio", {1, shape[1]}, "features"
                );
            }
            spdlog::info("HuBERT engine loaded: {}", hubert_path_.string());
        }
    } else {
        spdlog::warn("HuBERT model not found at {}", hubert_path_.string());
    }

    if (rmvpe_path_ && std::filesystem::exists(*rmvpe_path_)) {
        rmvpe_engine_ = make_engine(*rmvpe_path_);
        // RMVPE accepts fixed T or a dynamic optimization-profile axis.
        if (auto* trt = dynamic_cast<TrtEngine*>(rmvpe_engine_.get())) {
            const auto shape = trt->input_shape("mel");
            const bool ok = shape.empty()
                || (shape.size() == 3 && shape[0] == 1
                    && shape[1] == 128 && shape[2] != 0);
            if (!ok) {
                spdlog::warn("RMVPE TRT 引擎形状不符（期望 [1,128,T]），回退 ONNX");
                rmvpe_engine_.reset();
            }
        }
        if (rmvpe_engine_ && rmvpe_engine_->loaded()) {
            const auto shape = rmvpe_engine_->input_shape("mel");
            if (shape.size() == 3 && shape[2] > 0) {
                warmup_zeros(
                    *rmvpe_engine_, "mel", {1, 128, shape[2]}, "f0"
                );
            }
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

// 懒加载固定形状 TRT 引擎的动态 ONNX 兜底（file 模式任意长度输入）
IEngine* ensure_onnx_fallback(std::unique_ptr<IEngine>& slot,
                              const std::filesystem::path& onnx_path) {
    if (slot && slot->loaded()) return slot.get();
    auto onnx = std::make_unique<OnnxEngine>();
    if (!onnx->load(onnx_path)) return nullptr;
    spdlog::info("Lazy-loaded ONNX fallback: {}", onnx_path.filename().string());
    slot = std::move(onnx);
    return slot.get();
}

// 零输入预热：吸收 TRT 首调 JIT/惰性加载开销；ONNX+CUDA 不健康时在
// 加载期触发自愈降级而不是首个真实窗口
void warmup_zeros(IEngine& engine, const char* input,
                  std::vector<int64_t> shape, const char* output) {
    size_t elems = 1;
    for (auto d : shape) {
        if (d <= 0) return;
        elems *= static_cast<size_t>(d);
    }
    try {
        engine.run({input}, {shape}, {std::vector<float>(elems, 0.0f)}, {output});
    } catch (const std::exception& e) {
        spdlog::debug("Engine warmup skipped ({}): {}", input, e.what());
    }
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

    const int n_bins = n_fft / 2 + 1;
    thread_local std::vector<float> window;
    thread_local std::vector<float> mel_basis;
    thread_local std::vector<std::pair<int, int>> mel_ranges;
    thread_local int cached_fft = 0;
    thread_local int cached_mels = 0;
    thread_local uint32_t cached_sr = 0;
    if (cached_fft != n_fft || cached_mels != n_mels || cached_sr != sample_rate) {
        window.assign(n_fft, 0.0f);
        for (int i = 0; i < n_fft; ++i) {
            window[i] = 0.5f * (1.0f - std::cos(
                2.0f * static_cast<float>(M_PI) * i / n_fft));
        }
        mel_basis = build_htk_mel_basis(
            sample_rate, n_mels, n_fft, 30.0f,
            std::min(8000.0f, sample_rate / 2.0f));
        mel_ranges.assign(n_mels, {0, 0});
        for (int m = 0; m < n_mels; ++m) {
            const float* row = mel_basis.data() + static_cast<size_t>(m) * n_bins;
            int first = 0;
            while (first < n_bins && row[first] == 0.0f) ++first;
            int last = n_bins;
            while (last > first && row[last - 1] == 0.0f) --last;
            mel_ranges[m] = {first, last};
        }
        cached_fft = n_fft;
        cached_mels = n_mels;
        cached_sr = sample_rate;
    }
    std::vector<float> mel(static_cast<size_t>(frames) * n_mels, 0.0f);
    const float* window_values = window.data();
    const float* basis_values = mel_basis.data();
    const std::pair<int, int>* range_values = mel_ranges.data();

    // 每帧写入互不相交的 mel 行；单元素累加顺序不变 → 多线程逐位一致
    const auto frame_loop = [&](size_t f_begin, size_t f_end) {
        std::vector<float> re(n_fft), im(n_fft);
        std::vector<float> magnitude(n_bins);
        for (size_t f = f_begin; f < f_end; ++f) {
            const size_t off = f * static_cast<size_t>(hop_length);
            for (int i = 0; i < n_fft; ++i) {
                re[i] = padded[off + i] * window_values[i];
                im[i] = 0.0f;
            }
            fft_radix2(re, im);
            for (int k = 0; k < n_bins; ++k) {
                magnitude[k] = std::sqrt(re[k] * re[k] + im[k] * im[k]);
            }
            for (int m = 0; m < n_mels; ++m) {
                float value = 0.0f;
                const float* row = basis_values + static_cast<size_t>(m) * n_bins;
                const auto [first, last] = range_values[m];
                for (int k = first; k < last; ++k) {
                    value += row[k] * magnitude[k];
                }
                mel[f * n_mels + m] = std::log(std::max(value, 1e-5f));
            }
        }
    };

    const unsigned hw = std::thread::hardware_concurrency();
    const unsigned nthreads = std::min(4u, std::max(1u, hw));
    if (frames >= 64 && nthreads > 1) {
        std::vector<std::thread> pool;
        pool.reserve(nthreads);
        const size_t chunk = (frames + nthreads - 1) / nthreads;
        for (unsigned t = 0; t < nthreads; ++t) {
            const size_t begin = static_cast<size_t>(t) * chunk;
            const size_t end = std::min(frames, begin + chunk);
            if (begin >= end) break;
            pool.emplace_back(frame_loop, begin, end);
        }
        for (auto& th : pool) th.join();
    } else {
        frame_loop(0, frames);
    }
    return mel;
}

std::vector<float> FeatureExtractor::extract_mel(
    const std::vector<float>& audio,
    uint32_t sample_rate
) {
    return compute_mel(audio, sample_rate);
}

std::vector<float> FeatureExtractor::extract_f0(
    const std::vector<float>& audio,
    uint32_t sample_rate,
    const std::string& method
) {
    if (method == "rmvpe" && rmvpe_engine_ && rmvpe_engine_->loaded()) {
        auto mel = compute_mel(audio, sample_rate);
        constexpr size_t mel_bins = 128;
        const size_t mel_frames = mel.size() / mel_bins;
        std::vector<float> hz(mel_frames, 0.0f);
        constexpr size_t pitch_bins = 360;
        constexpr float cents_origin = 1997.3794084376191f;
        constexpr float salience_threshold = 0.03f;

        const auto decode_frame = [&](const std::vector<float>& salience, size_t frame) {
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
                return 0.0f;
            }
            return 10.0f * std::pow(2.0f, (cents_sum / weight_sum) / 1200.0f);
        };

        // A dynamic-length RMVPE export follows Python semantics exactly: pad
        // the log-mel time axis to a 32-multiple with zeros and run the whole
        // sequence in one model call. Windowed inference with a fixed
        // [1,128,128] export corrupts frames near every window edge because
        // the E2E U-Net has a receptive field far larger than the overlap.
        // A fixed-shape TRT engine takes the same single-pass path only when
        // its time axis equals the padded length (streaming contract);
        // other lengths (file mode) fall back to the dynamic ONNX engine.
        IEngine* rmvpe = rmvpe_engine_.get();
        bool dynamic_time = false;
        int64_t fixed_time = -1;
        if (rmvpe) {
            const auto mel_input_shape = rmvpe->input_shape("mel");
            if (mel_input_shape.size() >= 3) {
                if (mel_input_shape[2] < 0) dynamic_time = true;
                else if (mel_input_shape[2] > 0) fixed_time = mel_input_shape[2];
            }
        }
        const size_t padded_frames = mel_frames > 0
            ? 32 * ((mel_frames - 1) / 32 + 1) : 0;
        if (rmvpe && !dynamic_time
                && static_cast<size_t>(fixed_time) != padded_frames
                && ensure_onnx_fallback(rmvpe_onnx_engine_, *rmvpe_path_)) {
            spdlog::debug("RMVPE TRT fixed time {} != padded {}; using ONNX fallback",
                          fixed_time, padded_frames);
            rmvpe = rmvpe_onnx_engine_.get();
            dynamic_time = true;
        }
        if (rmvpe && (dynamic_time
                || static_cast<size_t>(fixed_time) == padded_frames)
                && mel_frames > 0) {
            std::vector<float> fixed_mel(padded_frames * mel_bins, 0.0f);
            for (size_t b = 0; b < mel_bins; ++b) {
                for (size_t t = 0; t < mel_frames; ++t) {
                    fixed_mel[b * padded_frames + t] = mel[t * mel_bins + b];
                }
            }
            const std::vector<int64_t> mel_shape = {
                1, static_cast<int64_t>(mel_bins), static_cast<int64_t>(padded_frames)
            };
            const auto salience = rmvpe->run(
                {"mel"}, {mel_shape}, {fixed_mel}, {"f0"}
            );
            if (salience.size() < padded_frames * pitch_bins) {
                throw std::runtime_error("RMVPE dynamic output smaller than [padded, 360]");
            }
            for (size_t t = 0; t < mel_frames; ++t) {
                hz[t] = decode_frame(salience, t);
            }
            spdlog::debug("RMVPE F0 (single-pass): {} frames extracted", hz.size());
            return hz;
        }

        // Fixed [batch, mel_bin, time]=[1,128,128] fallback: overlapping
        // windows so arbitrary-length files retain their final F0 frames.
        constexpr size_t rmvpe_frames = 128;
        constexpr size_t hop_frames = 96;
        constexpr size_t overlap_frames = rmvpe_frames - hop_frames;
        size_t written_until = 0;

        for (size_t frame_start = 0; frame_start < mel_frames; ) {
            // Right-align the final window. Otherwise a tail shorter than the
            // overlap would be discarded by keep_from below.
            if (mel_frames > rmvpe_frames
                && mel_frames - frame_start <= overlap_frames) {
                frame_start = mel_frames - rmvpe_frames;
            }
            // Python RMVPE pads the already-logarithmic mel tensor with zero.
            std::vector<float> fixed_mel(rmvpe_frames * mel_bins, 0.0f);
            const size_t copy_frames = std::min(rmvpe_frames, mel_frames - frame_start);
            for (size_t b = 0; b < mel_bins; ++b) {
                for (size_t t = 0; t < copy_frames; ++t) {
                    fixed_mel[b * rmvpe_frames + t] = mel[(frame_start + t) * mel_bins + b];
                }
            }

            const std::vector<int64_t> mel_shape = {
                1, static_cast<int64_t>(mel_bins), static_cast<int64_t>(rmvpe_frames)
            };
            const auto salience = rmvpe_engine_->run(
                {"mel"}, {mel_shape}, {fixed_mel}, {"f0"}
            );
            if (salience.size() < rmvpe_frames * pitch_bins) {
                throw std::runtime_error("RMVPE output shape is smaller than [128, 360]");
            }

            const size_t keep_from = written_until > frame_start
                ? written_until - frame_start : 0;
            for (size_t frame = keep_from; frame < copy_frames; ++frame) {
                hz[frame_start + frame] = decode_frame(salience, frame);
            }
            written_until = std::max(written_until, frame_start + copy_frames);
            if (frame_start + copy_frames >= mel_frames) break;
            const size_t next = frame_start + hop_frames;
            if (mel_frames - next <= overlap_frames) {
                frame_start = mel_frames - rmvpe_frames;
            } else {
                frame_start = next;
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
        // TRT 固定形状引擎只接受构建时长；其他长度走动态 ONNX 兜底
        IEngine* engine = hubert_engine_.get();
        if (auto* trt = dynamic_cast<TrtEngine*>(engine)) {
            const auto shape = trt->input_shape("audio");
            if (shape.size() == 2 && shape[1] > 0
                    && static_cast<size_t>(shape[1]) != n_samples
                    && ensure_onnx_fallback(hubert_onnx_engine_, hubert_path_)) {
                spdlog::debug("HuBERT TRT fixed length {} != {}; using ONNX fallback",
                              shape[1], n_samples);
                engine = hubert_onnx_engine_.get();
            }
        }
        std::vector<int64_t> audio_shape = {1, static_cast<int64_t>(n_samples)};
        auto feats = engine->run(
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
