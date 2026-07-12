#include <mozart/core/contract_packet.hpp>
#include <cassert>
#include <iostream>
#include <cmath>

using namespace mozart;

int main() {
    // Test 1: Contract packet round-trip
    {
        ContractPacket pkt;
        pkt.meta.pts_ns = 1234567890123ULL;
        pkt.meta.frame_idx = 42;
        pkt.meta.vad_flag = 1;
        pkt.meta.energy_db = 200;
        pkt.meta.conf = 255;
        pkt.meta.segment_id = 7;
        pkt.samples = {0.1f, 0.2f, -0.3f, 0.4f};

        auto packed = pkt.pack();
        assert(packed.size() == ContractPacket::HEADER_SIZE + 4 * sizeof(float));

        auto unpacked = ContractPacket::unpack(packed.data(), packed.size());
        assert(unpacked.has_value());
        assert(unpacked->meta.pts_ns == pkt.meta.pts_ns);
        assert(unpacked->meta.frame_idx == pkt.meta.frame_idx);
        assert(unpacked->meta.vad_flag == pkt.meta.vad_flag);
        assert(unpacked->meta.energy_db == pkt.meta.energy_db);
        assert(unpacked->meta.conf == pkt.meta.conf);
        assert(unpacked->meta.segment_id == pkt.meta.segment_id);
        assert(unpacked->samples.size() == pkt.samples.size());

        for (size_t i = 0; i < pkt.samples.size(); ++i) {
            assert(std::abs(unpacked->samples[i] - pkt.samples[i]) < 1e-6f);
        }

        std::cout << "[PASS] Contract packet round-trip\n";
    }

    // Test 2: Invalid magic rejection
    {
        uint8_t bad_data[100] = {0};
        bad_data[0] = 0xFF;  // Wrong magic
        auto result = ContractPacket::unpack(bad_data, sizeof(bad_data));
        assert(!result.has_value());
        std::cout << "[PASS] Invalid magic rejection\n";
    }

    // Test 3: Truncated packet rejection
    {
        uint8_t short_data[10] = {0};
        auto result = ContractPacket::unpack(short_data, sizeof(short_data));
        assert(!result.has_value());
        std::cout << "[PASS] Truncated packet rejection\n";
    }

    // Test 4: Silence detection
    {
        ContractPacket pkt;
        pkt.meta.vad_flag = 0;
        auto packed = pkt.pack();
        auto unpacked = ContractPacket::unpack(packed.data(), packed.size());
        assert(unpacked->is_silence());
        std::cout << "[PASS] Silence detection\n";
    }

    std::cout << "All contract_packet tests passed!\n";
    return 0;
}
