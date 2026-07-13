#include "network/packet.hpp"
#include <spdlog/spdlog.h>

namespace rvc {

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
