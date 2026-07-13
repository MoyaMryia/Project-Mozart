// mock_stream.hpp — Mock 音频流（测试驱动）
// ============================================================================
// 无需物理声卡与网络连接，直接读取测试用音频文件（如 noisy_sample.wav）
// 并产生伪 20ms 定时帧；用于 CTest 闭环集成测试。
//
// 采集端 (Capture)：从 WAV 文件循环读取，填充 mozart_raw_frame_t / mozart_input_frame_t
// 播放端 (Playback)：将帧 PCM 丢弃并计数（可校验写入帧数）
#ifndef MOZART_MOCK_STREAM_HPP
#define MOZART_MOCK_STREAM_HPP

#include "mozart/audio_stream.hpp"
#include <string>
#include <vector>
#include <atomic>
#include <cstdint>

namespace mozart {

class MockStream : public RealTimeAudioStream {
public:
    // direction = Capture: 从 wav_path 循环读取填充帧
    // direction = Playback: 写入帧被丢弃并计数
    MockStream(std::string wav_path, StreamDirection direction,
               uint32_t sample_rate = 48000, uint32_t frame_duration_ms = 20);
    ~MockStream() override;

    StreamDirection GetDirection() const noexcept override { return direction_; }
    bool IsOpen() const noexcept override { return open_; }

    bool Open(const StreamConfig& config) override;
    void Close() override;

    bool ReadFrame (void* out_frame_buf, uint32_t buf_size) override;
    bool WriteFrame(const void* in_frame_buf, uint32_t buf_size) override;

    uint64_t GetUnderlyingLatencyNs() const noexcept override { return 0; }

    // 测试观测口
    uint64_t frames_read()  const noexcept { return frames_read_.load(); }
    uint64_t frames_written() const noexcept { return frames_written_.load(); }

private:
    std::string        wav_path_;
    StreamDirection    direction_;
    uint32_t           sample_rate_;
    uint32_t           frame_duration_ms_;
    uint32_t           samples_per_frame_;

    bool               open_{false};
    std::vector<float> pcm_buffer_;     // Capture: 全量 PCM 数据
    size_t             read_cursor_{0}; // Capture: 当前读位置
    uint32_t           frame_idx_{0};

    std::atomic<uint64_t> frames_read_{0};
    std::atomic<uint64_t> frames_written_{0};

    bool load_wav();
};

} // namespace mozart

#endif // MOZART_MOCK_STREAM_HPP
