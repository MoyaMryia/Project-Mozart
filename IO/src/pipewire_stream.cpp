// pipewire_stream.cpp — PipeWire 物理设备驱动（stub 实现）
// ============================================================================
// 从 preprocessor/pipewire.c 迁移的设备 IO，当前为 stub（与原现状一致）：
//   Capture: ReadFrame 填充静音 PCM + 递增 frame_idx
//   Playback: WriteFrame 丢弃 PCM + 计数
//
// 真实 libpipewire 集成由 MOZART_IO_ENABLE_PIPEWIRE 编译选项启用，后续实现
// 将打开 pw_stream 在 capture/playback 模式，按 20ms quantum 收发契约帧。
#include "mozart/pipewire_stream.hpp"
#include "mozart/frame_meta.h"

#include <spdlog/spdlog.h>
#include <chrono>
#include <cstring>

namespace mozart {

PipeWireStream::PipeWireStream(std::string device_name, StreamDirection direction)
    : device_name_(std::move(device_name)), direction_(direction)
{}

PipeWireStream::~PipeWireStream() { Close(); }

bool PipeWireStream::Open(const StreamConfig& config) {
    if (open_) return true;
    if (config.direction != direction_
        || config.sample_rate != MOZART_RAW_SAMPLE_RATE
        || config.frame_duration_ms != MOZART_RAW_FRAME_MS) {
        return false;
    }
    sample_rate_        = config.sample_rate ? config.sample_rate : 48000;
    samples_per_frame_  = sample_rate_ * config.frame_duration_ms / 1000;
    if (samples_per_frame_ == 0) samples_per_frame_ = MOZART_RAW_SAMPLES;
    open_ = true;
    spdlog::info("PipeWireStream opened (stub): device='{}', dir={}, sr={}, spf={}",
                 device_name_.empty() ? "default" : device_name_,
                 direction_ == StreamDirection::Capture ? "capture" : "playback",
                 sample_rate_, samples_per_frame_);
    return true;
}

void PipeWireStream::Close() {
    if (!open_) return;
    open_ = false;
    spdlog::info("PipeWireStream closed: frames={}", frames_processed_.load());
}

bool PipeWireStream::ReadFrame(void* out_frame_buf, uint32_t buf_size) {
    if (!open_ || direction_ != StreamDirection::Capture) return false;

    // 校验 buf_size：采集流产出 mozart_raw_frame_t
    if (buf_size != sizeof(mozart_raw_frame_t)) {
        spdlog::warn("PipeWireStream ReadFrame: buf_size {} != {}", buf_size, sizeof(mozart_raw_frame_t));
        return false;
    }

    auto* frame = static_cast<mozart_raw_frame_t*>(out_frame_buf);
    // stub：填静音，元数据由 IO 模块填充（见 README §2.2）
    std::memset(frame->pcm, 0, sizeof(frame->pcm));
    frame->meta.pts_ns     = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count());
    frame->meta.frame_idx  = frame_idx_++;
    frame->meta.vad_flag   = 0;  // 静音，留待预处理判定
    frame->meta.energy_db  = 0;
    frame->meta.conf       = 0;
    frame->meta.segment_id = 0;

    frames_processed_.fetch_add(1, std::memory_order_relaxed);
    return true;
}

bool PipeWireStream::WriteFrame(const void* in_frame_buf, uint32_t buf_size) {
    if (!open_ || direction_ != StreamDirection::Playback) return false;
    if (buf_size != sizeof(mozart_output_frame_t)) {
        spdlog::warn("PipeWireStream WriteFrame: buf_size {} != {}", buf_size, sizeof(mozart_output_frame_t));
        return false;
    }
    (void)in_frame_buf;
    // stub：丢弃 PCM（真实实现将写入 pw_stream playback）
    frames_processed_.fetch_add(1, std::memory_order_relaxed);
    return true;
}

} // namespace mozart
