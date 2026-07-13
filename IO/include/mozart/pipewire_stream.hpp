// pipewire_stream.hpp — PipeWire 本地物理设备驱动
// ============================================================================
// 从 preprocessor/pipewire.{c,h} 抽出的设备 IO，迁入 IO 模块统一管理。
// 实现 RealTimeAudioStream：
//   Capture  → ReadFrame  产出 mozart_raw_frame_t    (48kHz / 960 samples)
//   Playback → WriteFrame 消费 mozart_output_frame_t (48kHz / 960 samples)
//
// 当前为 stub（与原 preprocessor/pipewire.c 现状一致）：采集填充静音 + 递增
// frame_idx；播放丢弃 PCM 并计数。真实 libpipewire 集成由 MOZART_IO_ENABLE_PIPEWIRE
// 编译选项与后续实现提供。
#ifndef MOZART_PIPEWIRE_STREAM_HPP
#define MOZART_PIPEWIRE_STREAM_HPP

#include "mozart/audio_stream.hpp"
#include <string>
#include <atomic>
#include <cstdint>

namespace mozart {

class PipeWireStream : public RealTimeAudioStream {
public:
    // device_name: PW node 名（NULL/空 = 默认设备）
    // direction: Capture = 麦克风采集, Playback = 虚拟源输出
    explicit PipeWireStream(std::string device_name, StreamDirection direction);
    ~PipeWireStream() override;

    StreamDirection GetDirection() const noexcept override { return direction_; }
    bool IsOpen() const noexcept override { return open_; }

    bool Open(const StreamConfig& config) override;
    void Close() override;

    bool ReadFrame (void* out_frame_buf, uint32_t buf_size) override;
    bool WriteFrame(const void* in_frame_buf, uint32_t buf_size) override;

    uint64_t GetUnderlyingLatencyNs() const noexcept override { return 0; }

    uint64_t frames_processed() const noexcept { return frames_processed_.load(); }

private:
    std::string     device_name_;
    StreamDirection direction_;
    uint32_t        sample_rate_{48000};
    uint32_t        samples_per_frame_{960};
    bool            open_{false};
    uint32_t        frame_idx_{0};

    std::atomic<uint64_t> frames_processed_{0};
};

} // namespace mozart

#endif // MOZART_PIPEWIRE_STREAM_HPP
