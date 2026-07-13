// audio_stream.cpp — C-ABI 流工厂分发与生命周期桥接
// ============================================================================
// 实现 audio_io.h 的 mozart_io_create_*_stream / destroy / read_frame /
// write_frame / get_underlying_latency_ns。具体驱动实现见各自 cpp。
#include "mozart/audio_io.h"
#include "mozart/audio_stream.hpp"
#include "mozart/pipewire_stream.hpp"
#ifdef MOZART_IO_ENABLE_UDP
#include "mozart/udp_stream.hpp"
#endif

#include <spdlog/spdlog.h>
#include <utility>
namespace {

// 句柄 ↔ C++ 对象的转换辅助
inline mozart::RealTimeAudioStream* to_rt(mozart_stream_handle_t h) {
    return static_cast<mozart::RealTimeAudioStream*>(h);
}

inline mozart::StreamDirection to_dir(int direction) {
    return (direction == MOZART_IO_DIR_PLAYBACK)
        ? mozart::StreamDirection::Playback
        : mozart::StreamDirection::Capture;
}

inline bool valid_direction(int direction) {
    return direction == MOZART_IO_DIR_CAPTURE
        || direction == MOZART_IO_DIR_PLAYBACK;
}

template <typename Result, typename Function>
Result cabi_guard(Result fallback, Function&& function) noexcept {
    try {
        return std::forward<Function>(function)();
    } catch (...) {
        return fallback;
    }
}

template <typename Function>
void cabi_guard_void(Function&& function) noexcept {
    try {
        std::forward<Function>(function)();
    } catch (...) {
    }
}

} // namespace

extern "C" {

MOZART_API mozart_stream_handle_t mozart_io_create_pipewire_stream(const char* device_name,
                                                                   int direction) {
    if (!valid_direction(direction)) return nullptr;
    return cabi_guard<mozart_stream_handle_t>(nullptr, [&] {
        auto* s = new mozart::PipeWireStream(
            device_name ? device_name : "", to_dir(direction));
        return static_cast<mozart_stream_handle_t>(s);
    });
}

MOZART_API mozart_stream_handle_t mozart_io_create_udp_stream(const char* host,
                                                              uint16_t port,
                                                              int direction) {
#ifdef MOZART_IO_ENABLE_UDP
    if (!valid_direction(direction)) return nullptr;
    return cabi_guard<mozart_stream_handle_t>(nullptr, [&] {
        auto* s = new mozart::UdpStream(
            host ? host : "0.0.0.0", port, to_dir(direction));
        return static_cast<mozart_stream_handle_t>(s);
    });
#else
    (void)host; (void)port; (void)direction;
    spdlog::warn("mozart_io_create_udp_stream: UDP disabled at build time");
    return nullptr;
#endif
}

MOZART_API bool mozart_io_open_stream(mozart_stream_handle_t handle,
                                      uint32_t sample_rate,
                                      uint32_t frame_duration_ms,
                                      uint32_t ring_capacity) {
    if (!handle || sample_rate == 0 || frame_duration_ms == 0) return false;
    return cabi_guard(false, [&] {
        auto* stream = static_cast<mozart::AudioStream*>(handle);
        mozart::StreamConfig config;
        config.direction = stream->GetDirection();
        config.sample_rate = sample_rate;
        config.frame_duration_ms = frame_duration_ms;
        config.ring_capacity = ring_capacity == 0 ? 16 : ring_capacity;
        return stream->Open(config);
    });
}

MOZART_API void mozart_io_close_stream(mozart_stream_handle_t handle) {
    if (!handle) return;
    cabi_guard_void([&] { static_cast<mozart::AudioStream*>(handle)->Close(); });
}

MOZART_API bool mozart_io_is_stream_open(mozart_stream_handle_t handle) {
    if (!handle) return false;
    return cabi_guard(false, [&] {
        return static_cast<mozart::AudioStream*>(handle)->IsOpen();
    });
}

MOZART_API void mozart_io_destroy_stream(mozart_stream_handle_t handle) {
    if (!handle) return;
    cabi_guard_void([&] { delete static_cast<mozart::AudioStream*>(handle); });
}

MOZART_API bool mozart_io_read_frame(mozart_stream_handle_t handle,
                                     void* out_frame_buf, uint32_t buf_size) {
    if (!handle || !out_frame_buf) return false;
    return cabi_guard(false, [&] {
        return to_rt(handle)->ReadFrame(out_frame_buf, buf_size);
    });
}

MOZART_API bool mozart_io_write_frame(mozart_stream_handle_t handle,
                                      const void* in_frame_buf, uint32_t buf_size) {
    if (!handle || !in_frame_buf) return false;
    return cabi_guard(false, [&] {
        return to_rt(handle)->WriteFrame(in_frame_buf, buf_size);
    });
}

MOZART_API uint64_t mozart_io_get_underlying_latency_ns(mozart_stream_handle_t handle) {
    if (!handle) return 0;
    return cabi_guard<uint64_t>(0, [&] {
        return to_rt(handle)->GetUnderlyingLatencyNs();
    });
}

} // extern "C"
