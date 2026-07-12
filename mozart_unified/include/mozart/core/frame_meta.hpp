#pragma once
#include <cstdint>
#include <cstring>
#include <array>

namespace mozart {

// Per-frame metadata sidecar for the Mozart contract boundary.
// 16 bytes packed (little-endian), must match Rust #[repr(C)] struct.
//
// Note: Original documentation said "12 bytes" but the fields sum to 16.
// This is the corrected version.
#pragma pack(push, 1)
struct FrameMeta {
    uint64_t pts_ns;       // Presentation timestamp in nanoseconds
    uint32_t frame_idx;    // Monotonic frame counter from preprocessing
    uint8_t  vad_flag;     // 0 = silence, 1 = voice
    uint8_t  energy_db;    // Frame energy in dB (0-255 quantized)
    uint8_t  conf;         // Denoiser confidence (0-255), post can downweight
    uint8_t  segment_id;   // Voice segment ID (0 = silence gap)
};
#pragma pack(pop)

static_assert(sizeof(FrameMeta) == 16, "FrameMeta must be 16 bytes with packing");
static_assert(alignof(FrameMeta) == 1, "FrameMeta must be packed");

// Convert raw byte buffer (>=16 bytes) into a FrameMeta without UB.
inline FrameMeta frame_meta_from_bytes(const void* data) {
    FrameMeta m{};
    const auto* p = static_cast<const uint8_t*>(data);
    std::memcpy(&m, p, sizeof(m));
    return m;
}

// Audio frame buffer (input from preprocessing)
struct FrameBuf {
    std::array<float, 320> samples;  // 20ms @ 16kHz = 320 samples
    FrameMeta meta;
};

// Output frame buffer (to output sink)
struct OutputFrameBuf {
    std::array<float, 960> samples;  // 20ms @ 48kHz = 960 samples
};

// Constants
constexpr size_t kContractSamples = 320;  // Input frame size @ 16kHz
constexpr size_t kOutputSamples = 960;    // Output frame size @ 48kHz
constexpr uint32_t kOutputRate = 48000;
constexpr uint32_t kContractRate = 16000;

}  // namespace mozart
