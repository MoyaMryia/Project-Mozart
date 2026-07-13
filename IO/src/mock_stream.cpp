// mock_stream.cpp — Mock 音频流实现（测试驱动）
// ============================================================================
// Capture: 从 WAV 文件加载全量 PCM，按 20ms 帧切片循环填充契约帧
// Playback: 写入帧 PCM 丢弃并计数
// 用于 CTest 闭环测试，无物理设备/网络依赖。
#include "mozart/mock_stream.hpp"
#include "mozart/frame_meta.h"

#include <spdlog/spdlog.h>
#include <fstream>
#include <cstring>
#include <cmath>

namespace mozart {

namespace {

// 简易 WAV 解析：支持 PCM(int16) 与 float32，mono/stereo，返回 float32 mono
bool load_wav_f32_mono(const std::string& path, std::vector<float>& out,
                       uint32_t& out_sample_rate) {
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        spdlog::warn("MockStream: cannot open WAV '{}'", path);
        return false;
    }

    char riff[4]; f.read(riff, 4);
    if (std::memcmp(riff, "RIFF", 4) != 0) { spdlog::warn("MockStream: not RIFF"); return false; }
    f.seekg(4, std::ios::cur); // file size
    char wave[4]; f.read(wave, 4);
    if (std::memcmp(wave, "WAVE", 4) != 0) { spdlog::warn("MockStream: not WAVE"); return false; }

    uint16_t audio_format = 0, channels = 0, bits = 0;
    uint32_t sample_rate = 0;
    std::vector<unsigned char> data;

    while (f) {
        char id[4]; f.read(id, 4);
        if (f.gcount() != 4) break;
        uint32_t sz = 0; f.read(reinterpret_cast<char*>(&sz), 4);
        if (std::memcmp(id, "fmt ", 4) == 0) {
            f.read(reinterpret_cast<char*>(&audio_format), 2);
            f.read(reinterpret_cast<char*>(&channels), 2);
            f.read(reinterpret_cast<char*>(&sample_rate), 4);
            f.seekg(4, std::ios::cur); // byte rate
            f.seekg(2, std::ios::cur); // block align
            f.read(reinterpret_cast<char*>(&bits), 2);
            if (sz > 16) f.seekg(sz - 16, std::ios::cur);
        } else if (std::memcmp(id, "data", 4) == 0) {
            data.resize(sz);
            f.read(reinterpret_cast<char*>(data.data()), sz);
        } else {
            f.seekg(sz, std::ios::cur);
        }
        if (sz & 1) f.seekg(1, std::ios::cur); // padding byte
    }

    if (data.empty()) { spdlog::warn("MockStream: no data chunk"); return false; }
    if (channels == 0 || bits == 0 || sample_rate == 0) return false;

    out_sample_rate = sample_rate;
    const size_t total = data.size() / (channels * (bits / 8));
    out.resize(total);

    auto read_sample = [&](size_t i) -> float {
        size_t off = i * channels * (bits / 8);
        if (audio_format == 3 && bits == 32) {  // IEEE float32
            float v; std::memcpy(&v, data.data() + off, 4);
            return v;
        } else if (audio_format == 1 && bits == 16) {  // PCM int16
            int16_t v; std::memcpy(&v, data.data() + off, 2);
            return static_cast<float>(v) / 32768.0f;
        } else if (audio_format == 1 && bits == 24) {
            const unsigned char* p = data.data() + off;
            int32_t v = static_cast<int32_t>(p[0]) | (static_cast<int32_t>(p[1]) << 8)
                      | (p[2] & 0x80 ? (static_cast<int32_t>(p[2]) << 16) | 0xFF000000
                                     : (static_cast<int32_t>(p[2]) << 16));
            return static_cast<float>(v) / 8388608.0f;
        }
        return 0.0f;
    };

    for (size_t i = 0; i < total; ++i) {
        // 多声道 → mono：取第 0 声道（简化，避免混音复杂度）
        out[i] = read_sample(i);
    }
    return true;
}

} // namespace

MockStream::MockStream(std::string wav_path, StreamDirection direction,
                       uint32_t sample_rate, uint32_t frame_duration_ms)
    : wav_path_(std::move(wav_path))
    , direction_(direction)
    , sample_rate_(sample_rate)
    , frame_duration_ms_(frame_duration_ms)
    , samples_per_frame_(sample_rate * frame_duration_ms / 1000)
{}

MockStream::~MockStream() { Close(); }

bool MockStream::Open(const StreamConfig& config) {
    if (open_) return true;
    sample_rate_       = config.sample_rate;
    frame_duration_ms_ = config.frame_duration_ms;
    samples_per_frame_ = sample_rate_ * frame_duration_ms_ / 1000;

    if (direction_ == StreamDirection::Capture) {
        uint32_t file_sr = sample_rate_;
        if (!load_wav_f32_mono(wav_path_, pcm_buffer_, file_sr)) {
            spdlog::error("MockStream: failed to load '{}'", wav_path_);
            return false;
        }
        if (file_sr != sample_rate_) {
            spdlog::warn("MockStream: WAV sample rate {} != requested {}, using file sr",
                         file_sr, sample_rate_);
            sample_rate_ = file_sr;
            samples_per_frame_ = sample_rate_ * frame_duration_ms_ / 1000;
        }
        if (pcm_buffer_.empty()) {
            spdlog::error("MockStream: empty PCM buffer");
            return false;
        }
    }
    open_ = true;
    spdlog::info("MockStream opened: dir={}, sr={}, spf={}",
                 direction_ == StreamDirection::Capture ? "capture" : "playback",
                 sample_rate_, samples_per_frame_);
    return true;
}

void MockStream::Close() {
    if (!open_) return;
    open_ = false;
    pcm_buffer_.clear();
    read_cursor_ = 0;
}

bool MockStream::ReadFrame(void* out_frame_buf, uint32_t buf_size) {
    if (!open_ || direction_ != StreamDirection::Capture) return false;

    // 校验 buf_size 与契约帧大小
    const uint32_t expected_input  = sizeof(mozart_input_frame_t);
    const uint32_t expected_raw    = sizeof(mozart_raw_frame_t);
    if (buf_size != expected_input && buf_size != expected_raw) {
        spdlog::warn("MockStream ReadFrame: buf_size {} mismatch (input={} raw={})",
                     buf_size, expected_input, expected_raw);
        return false;
    }

    const uint32_t spf = buf_size == expected_raw
        ? MOZART_RAW_SAMPLES
        : MOZART_INPUT_SAMPLES;

    // 取帧体偏移（跳过 16B meta）
    auto* base = static_cast<unsigned char*>(out_frame_buf);
    float* pcm = reinterpret_cast<float*>(base + 16);
    // 填充 PCM（循环读）
    for (uint32_t i = 0; i < spf; ++i) {
        if (read_cursor_ >= pcm_buffer_.size()) read_cursor_ = 0;
        pcm[i] = pcm_buffer_[read_cursor_++];
    }

    // 填充 meta
    auto* meta = reinterpret_cast<mozart_frame_meta_t*>(base);
    meta->pts_ns     = 0;  // Mock 不产生真实时间戳
    meta->frame_idx  = frame_idx_++;
    meta->vad_flag   = 1;  // Mock 默认有声
    meta->energy_db  = 128;
    meta->conf       = 255;
    meta->segment_id = 1;

    frames_read_.fetch_add(1, std::memory_order_relaxed);
    return true;
}

bool MockStream::WriteFrame(const void* in_frame_buf, uint32_t buf_size) {
    if (!open_ || direction_ != StreamDirection::Playback) return false;
    if (!in_frame_buf || buf_size != sizeof(mozart_output_frame_t)) return false;
    (void)in_frame_buf;
    frames_written_.fetch_add(1, std::memory_order_relaxed);
    return true;
}

} // namespace mozart
