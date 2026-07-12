#include <mozart/core/contract_packet.hpp>
#include <cstring>

namespace mozart {

// ── Little-endian helpers ─────────────────────────────────────────────────────
static void write_le32(uint8_t* buf, uint32_t v) {
    buf[0] = v & 0xFF;
    buf[1] = (v >> 8) & 0xFF;
    buf[2] = (v >> 16) & 0xFF;
    buf[3] = (v >> 24) & 0xFF;
}

static void write_le64(uint8_t* buf, uint64_t v) {
    for (int i = 0; i < 8; ++i) {
        buf[i] = (v >> (i * 8)) & 0xFF;
    }
}

static uint32_t read_le32(const uint8_t* buf) {
    return (uint32_t)buf[0] | ((uint32_t)buf[1] << 8) |
           ((uint32_t)buf[2] << 16) | ((uint32_t)buf[3] << 24);
}

static uint64_t read_le64(const uint8_t* buf) {
    uint64_t v = 0;
    for (int i = 0; i < 8; ++i) {
        v |= ((uint64_t)buf[i]) << (i * 8);
    }
    return v;
}

// ── ContractPacket ────────────────────────────────────────────────────────────
std::vector<uint8_t> ContractPacket::pack() const {
    size_t total = HEADER_SIZE + samples.size() * sizeof(float);
    std::vector<uint8_t> buf(total);

    // Magic
    write_le32(buf.data(), MAGIC);

    // FrameMeta (16 bytes)
    write_le64(buf.data() + 4, meta.pts_ns);
    write_le32(buf.data() + 12, meta.frame_idx);
    buf[16] = meta.vad_flag;
    buf[17] = meta.energy_db;
    buf[18] = meta.conf;
    buf[19] = meta.segment_id;

    // Samples (float32 little-endian)
    std::memcpy(buf.data() + HEADER_SIZE, samples.data(), samples.size() * sizeof(float));

    return buf;
}

std::optional<ContractPacket> ContractPacket::unpack(const uint8_t* data, size_t len) {
    if (len < HEADER_SIZE) return std::nullopt;

    uint32_t magic = read_le32(data);
    if (magic != MAGIC) return std::nullopt;

    ContractPacket pkt;
    pkt.meta.pts_ns = read_le64(data + 4);
    pkt.meta.frame_idx = read_le32(data + 12);
    pkt.meta.vad_flag = data[16];
    pkt.meta.energy_db = data[17];
    pkt.meta.conf = data[18];
    pkt.meta.segment_id = data[19];

    size_t sample_bytes = len - HEADER_SIZE;
    if (sample_bytes % sizeof(float) != 0) return std::nullopt;

    size_t num_samples = sample_bytes / sizeof(float);
    pkt.samples.resize(num_samples);
    std::memcpy(pkt.samples.data(), data + HEADER_SIZE, sample_bytes);

    return pkt;
}

// ── LegacyPacket ──────────────────────────────────────────────────────────────
std::vector<uint8_t> LegacyPacket::pack() const {
    size_t total = HEADER_SIZE + samples.size() * sizeof(int16_t);
    std::vector<uint8_t> buf(total);

    write_le32(buf.data(), MAGIC);
    write_le32(buf.data() + 4, seq);
    write_le64(buf.data() + 8, timestamp_us);
    write_le32(buf.data() + 16, format_code);

    std::memcpy(buf.data() + HEADER_SIZE, samples.data(), samples.size() * sizeof(int16_t));

    return buf;
}

std::optional<LegacyPacket> LegacyPacket::unpack(const uint8_t* data, size_t len) {
    if (len < HEADER_SIZE) return std::nullopt;

    uint32_t magic = read_le32(data);
    if (magic != MAGIC) return std::nullopt;

    LegacyPacket pkt;
    pkt.seq = read_le32(data + 4);
    pkt.timestamp_us = read_le64(data + 8);
    pkt.format_code = read_le32(data + 16);

    size_t sample_bytes = len - HEADER_SIZE;
    if (sample_bytes % sizeof(int16_t) != 0) return std::nullopt;

    size_t num_samples = sample_bytes / sizeof(int16_t);
    pkt.samples.resize(num_samples);
    std::memcpy(pkt.samples.data(), data + HEADER_SIZE, sample_bytes);

    return pkt;
}

}  // namespace mozart
