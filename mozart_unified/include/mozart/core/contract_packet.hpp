#pragma once
#include <cstdint>
#include <vector>
#include <optional>
#include "frame_meta.hpp"

namespace mozart {

// Contract audio packet for UDP transport (MZRT format)
// 20-byte header + float32 PCM samples
struct ContractPacket {
    static constexpr uint32_t MAGIC = 0x4D5A5254;  // 'MZRT'
    static constexpr size_t HEADER_SIZE = 20;

    FrameMeta meta;
    std::vector<float> samples;  // float32 mono, range [-1.0, 1.0]

    // Pack to bytes (little-endian)
    std::vector<uint8_t> pack() const;

    // Unpack from bytes. Returns nullopt if invalid.
    static std::optional<ContractPacket> unpack(const uint8_t* data, size_t len);

    // Check if frame is silence (vad_flag == 0)
    bool is_silence() const { return meta.vad_flag == 0; }
};

// Legacy packet format (RAVC, int16, 48kHz) - kept for backward compatibility
struct LegacyPacket {
    static constexpr uint32_t MAGIC = 0x52415643;  // 'RAVC'
    static constexpr size_t HEADER_SIZE = 20;

    uint32_t seq = 0;
    uint64_t timestamp_us = 0;
    uint16_t format_code = 0;
    std::vector<int16_t> samples;

    std::vector<uint8_t> pack() const;
    static std::optional<LegacyPacket> unpack(const uint8_t* data, size_t len);
};

}  // namespace mozart
