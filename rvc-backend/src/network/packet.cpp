#include "network/packet.hpp"
#include <spdlog/spdlog.h>

namespace rvc {

// ──────────────────────────────────────────────────────────
// LegacyAudioPacket
// ──────────────────────────────────────────────────────────
std::vector<uint8_t> LegacyAudioPacket::pack() const {
    std::vector<uint8_t> data;
    data.resize(HEADER_SIZE + samples.size() * 2);

    write_u32_le(data.data(), MAGIC);
    write_u32_le(data.data() + 4, seq);
    write_u64_le(data.data() + 8, timestamp_us);
    write_u32_le(data.data() + 16, format_code | (static_cast<uint32_t>(samples.size()) << 16));

    for (size_t i = 0; i < samples.size(); ++i) {
        int16_t s = samples[i];
        data[HEADER_SIZE + i * 2] = static_cast<uint8_t>(s);
        data[HEADER_SIZE + i * 2 + 1] = static_cast<uint8_t>(s >> 8);
    }
    return data;
}

std::optional<LegacyAudioPacket> LegacyAudioPacket::unpack(const uint8_t* data, size_t len) {
    if (len < HEADER_SIZE) return std::nullopt;

    uint32_t magic = read_u32_le(data);
    if (magic != MAGIC) return std::nullopt;

    uint32_t seq = read_u32_le(data + 4);
    uint64_t timestamp = read_u64_le(data + 8);
    uint32_t fmt_samples = read_u32_le(data + 16);
    uint16_t format_code = static_cast<uint16_t>(fmt_samples);
    uint16_t samples_count = static_cast<uint16_t>(fmt_samples >> 16);

    size_t expected = HEADER_SIZE + samples_count * 2;
    if (len < expected) return std::nullopt;

    LegacyAudioPacket pkt;
    pkt.seq = seq;
    pkt.timestamp_us = timestamp;
    pkt.format_code = format_code;
    pkt.samples.resize(samples_count);
    for (size_t i = 0; i < samples_count; ++i) {
        pkt.samples[i] = static_cast<int16_t>(data[HEADER_SIZE + i * 2] | (data[HEADER_SIZE + i * 2 + 1] << 8));
    }
    return pkt;
}

// ──────────────────────────────────────────────────────────
// ContractAudioPacket
// ──────────────────────────────────────────────────────────
std::vector<uint8_t> ContractAudioPacket::pack() const {
    size_t payload_bytes = samples.size() * sizeof(float);
    std::vector<uint8_t> data;
    data.resize(HEADER_SIZE + payload_bytes);

    write_u32_le(data.data(), MAGIC);
    write_u64_le(data.data() + 4, meta.pts_ns);
    write_u32_le(data.data() + 12, meta.frame_idx);
    data[16] = meta.vad_flag;
    data[17] = meta.energy_db;
    data[18] = meta.conf;
    data[19] = meta.segment_id;

    std::memcpy(data.data() + HEADER_SIZE, samples.data(), payload_bytes);
    return data;
}

std::optional<ContractAudioPacket> ContractAudioPacket::unpack(
    const uint8_t* data, size_t len,
    std::optional<size_t> expected_samples
) {
    if (len < HEADER_SIZE) return std::nullopt;

    uint32_t magic = read_u32_le(data);
    if (magic != MAGIC) return std::nullopt;

    size_t payload_bytes = len - HEADER_SIZE;
    if (payload_bytes % sizeof(float) != 0) return std::nullopt;
    size_t sample_count = payload_bytes / sizeof(float);

    if (expected_samples.has_value() && sample_count != expected_samples.value()) {
        spdlog::warn("Contract packet sample count mismatch: got {}, expected {}",
                     sample_count, expected_samples.value());
        // Still accept but log warning
    }

    ContractAudioPacket pkt;
    pkt.meta.pts_ns = read_u64_le(data + 4);
    pkt.meta.frame_idx = read_u32_le(data + 12);
    pkt.meta.vad_flag = data[16];
    pkt.meta.energy_db = data[17];
    pkt.meta.conf = data[18];
    pkt.meta.segment_id = data[19];

    pkt.samples.resize(sample_count);
    std::memcpy(pkt.samples.data(), data + HEADER_SIZE, payload_bytes);

    return pkt;
}

double ContractAudioPacket::duration_ms(uint32_t sample_rate) const {
    return static_cast<double>(samples.size()) / static_cast<double>(sample_rate) * 1000.0;
}

ContractAudioPacket ContractAudioPacket::from_samples(
    uint64_t pts_ns, uint32_t frame_idx,
    const std::vector<float>& samples,
    uint8_t vad_flag, uint8_t energy_db, uint8_t conf, uint8_t segment_id
) {
    ContractAudioPacket pkt;
    pkt.meta = {pts_ns, frame_idx, vad_flag, energy_db, conf, segment_id};
    pkt.samples = samples;
    return pkt;
}

} // namespace rvc
