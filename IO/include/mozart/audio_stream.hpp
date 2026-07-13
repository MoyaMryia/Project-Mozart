// audio_stream.hpp — C++ 音频流抽象层
// ============================================================================
// 对应 README §3 的设计：
//   AudioStream            — 顶层抽象（Open/Close/GetType/GetDirection/IsOpen）
//   RealTimeAudioStream    — 实时帧驱动（PipeWire / UDP）
//   OfflineAudioStream     — 离线块驱动（WAV 文件 / WebSocket）
//
// C-ABI（audio_io.h）的 mozart_io_create_*_stream 工厂在内部构造具体子类
// 并返回不透明句柄；C++ 使用者可直接持有具体子类指针。
//
// 帧类型约定（与 frame_meta.h 对齐）：
//   PipeWire Capture  → ReadFrame  产出 mozart_raw_frame_t    (48kHz / 3856B)
//   UDP Capture       → ReadFrame  产出 mozart_input_frame_t  (16kHz / 1296B)
//   PipeWire Playback → WriteFrame 消费 mozart_output_frame_t (48kHz / 3856B)
//   UDP Playback      → WriteFrame 消费 mozart_output_frame_t (48kHz / 3856B)
#ifndef MOZART_AUDIO_STREAM_HPP
#define MOZART_AUDIO_STREAM_HPP

#include <cstdint>
#include <cstddef>
#include "mozart/frame_meta.h"

namespace mozart {

enum class StreamType : int {
    RealTime = 0,
    Offline   = 1
};

enum class StreamDirection : int {
    Capture  = 0,
    Playback = 1
};

// 打开流时的配置（由 status_manager 在编排时填充后下发给 IO 模块）
struct StreamConfig {
    StreamDirection direction = StreamDirection::Capture;
    uint32_t        sample_rate       = 48000;   // 期望采样率（用于校验/重采样）
    uint32_t        frame_duration_ms = 20;
    uint32_t        ring_capacity     = 16;      // 内部预分配内存池帧数
};

// ---- 顶层抽象 ----------------------------------------------------------------
class AudioStream {
public:
    virtual ~AudioStream() = default;

    virtual StreamType      GetType()      const noexcept = 0;
    virtual StreamDirection GetDirection() const noexcept = 0;
    virtual bool IsOpen()   const noexcept = 0;

    // 打开流并预分配内存池（实时流零运行时 malloc 的前提）
    virtual bool Open(const StreamConfig& config) = 0;
    virtual void Close() = 0;
};

// ---- 实时帧驱动流 ------------------------------------------------------------
class RealTimeAudioStream : public AudioStream {
public:
    StreamType GetType() const noexcept override { return StreamType::RealTime; }

    // 阻塞式读/写单帧；buf_size 必须与具体子类期望的帧大小一致
    virtual bool ReadFrame (void* out_frame_buf, uint32_t buf_size) = 0;
    virtual bool WriteFrame(const void* in_frame_buf, uint32_t buf_size) = 0;

    // 底层设备/协议延迟（纳秒），不支持时返回 0
    virtual uint64_t GetUnderlyingLatencyNs() const noexcept = 0;
};

// ---- 离线块驱动流 ------------------------------------------------------------
class OfflineAudioStream : public AudioStream {
public:
    StreamType GetType() const noexcept override { return StreamType::Offline; }

    // 一次性读/写整块不规则音频 buffer
    virtual size_t ReadChunk (float* out_pcm, size_t max_samples,
                              mozart_frame_meta_t& out_meta) = 0;
    virtual size_t WriteChunk(const float* in_pcm, size_t samples,
                              const mozart_frame_meta_t& meta) = 0;
};

} // namespace mozart

#endif // MOZART_AUDIO_STREAM_HPP
