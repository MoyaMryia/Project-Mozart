// frame_meta.hpp
// 12-byte per-frame metadata, ABI-stable across the Mozart contract boundary.
// MUST stay in sync with the Rust #[repr(C)] struct on the preprocessing side.
#pragma once

#include <cstdint>
#include <cstring>

namespace mozart {

#pragma pack(push, 1)
struct FrameMeta {
    uint64_t pts_ns;       // presentation timestamp, nanoseconds since epoch
    uint32_t frame_idx;    // monotonic frame counter from preprocessing
    uint8_t  vad_flag;     // 0 = silence, 1 = voice
    uint8_t  energy_db;    // frame energy in dB (0-255 quantized)
    uint8_t  conf;         // denoiser confidence (0-255), post can downweight
    uint8_t  segment_id;   // voice segment id (0 = silence gap)
};
#pragma pack(pop)

static_assert(sizeof(FrameMeta) == 12, "FrameMeta must be 12 bytes");
static_assert(alignof(FrameMeta) == 1, "FrameMeta must be packed");

// Convert raw byte buffer (>=12 bytes) into a FrameMeta without UB.
inline FrameMeta frame_meta_from_bytes(const void* data) {
    FrameMeta m{};
    const auto* p = static_cast<const uint8_t*>(data);
    std::memcpy(&m, p, sizeof(m));
    return m;
}

} // namespace mozart