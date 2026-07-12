#pragma once

#include <cstdint>
#include <vector>
#include <optional>
#include <cstring>
#include "common.hpp"

namespace rvc {

// ──────────────────────────────────────────────────────────
// Legacy packet format (int16, 48kHz, backward compatible)
// ──────────────────────────────────────────────────────────
struct LegacyAudioPacket {
    uint32_t seq = 0;
    uint64_t timestamp_us = 0;
    uint16_t format_code = 0;
    std::vector<int16_t> samples;

    static constexpr uint32_t MAGIC = 0x52415643; // 'RAVC'
    static constexpr size_t HEADER_SIZE = 20;

    std::vector<uint8_t> pack() const;
    static std::optional<LegacyAudioPacket> unpack(const uint8_t* data, size_t len);
};

// ──────────────────────────────────────────────────────────
// Contract stream packet (float32, from Project Mozart)
// ──────────────────────────────────────────────────────────
struct FrameMeta {
    uint64_t pts_ns = 0;       // presentation timestamp in nanoseconds
    uint32_t frame_idx = 0;    // monotonically increasing frame index
    uint8_t  vad_flag = 0;     // 1 = contains speech, 0 = silence
    uint8_t  energy_db = 0;    // energy mapped to 0-255
    uint8_t  conf = 0;         // preprocessor confidence 0-255
    uint8_t  segment_id = 0;   // speech segment id (0 = silence)
};

struct ContractAudioPacket {
    FrameMeta meta;
    std::vector<float> samples; // float32 mono, range [-1.0, 1.0]

    static constexpr uint32_t MAGIC = 0x4D5A5254; // 'MZRT'
    static constexpr size_t HEADER_SIZE = 20;

    // Pack to bytes (little-endian)
    std::vector<uint8_t> pack() const;

    // Unpack from bytes. If expected_samples is provided, validate exact count.
    static std::optional<ContractAudioPacket> unpack(
        const uint8_t* data, size_t len,
        std::optional<size_t> expected_samples = std::nullopt
    );

    // Duration in milliseconds for a given sample rate
    double duration_ms(uint32_t sample_rate) const;
    bool is_silence() const { return meta.vad_flag == 0; }

    // Convenience: create from float samples
    static ContractAudioPacket from_samples(
        uint64_t pts_ns, uint32_t frame_idx,
        const std::vector<float>& samples,
        uint8_t vad_flag = 1,
        uint8_t energy_db = 128,
        uint8_t conf = 255,
        uint8_t segment_id = 1
    );
};

} // namespace rvc
