#include <iostream>
#include <cassert>
#include <cmath>
#include "network/packet.hpp"

using namespace rvc;

void test_contract_packet_roundtrip() {
    std::vector<float> samples(320);
    for (size_t i = 0; i < 320; ++i) {
        samples[i] = std::sin(2.0f * 3.14159f * 440.0f * static_cast<float>(i) / 16000.0f) * 0.5f;
    }

    ContractAudioPacket original;
    original.meta = {1234567890ULL, 42, 1, 200, 255, 5};
    original.samples = samples;

    auto packed = original.pack();
    assert(packed.size() == ContractAudioPacket::HEADER_SIZE + 320 * sizeof(float));

    auto unpacked = ContractAudioPacket::unpack(packed.data(), packed.size(), 320);
    assert(unpacked.has_value());
    assert(unpacked->meta.pts_ns == 1234567890ULL);
    assert(unpacked->meta.frame_idx == 42);
    assert(unpacked->meta.vad_flag == 1);
    assert(unpacked->meta.energy_db == 200);
    assert(unpacked->meta.conf == 255);
    assert(unpacked->meta.segment_id == 5);
    assert(unpacked->samples.size() == 320);

    for (size_t i = 0; i < 320; ++i) {
        assert(std::abs(unpacked->samples[i] - samples[i]) < 1e-6f);
    }

    std::cout << "test_contract_packet_roundtrip PASSED\n";
}

void test_contract_invalid_magic() {
    uint8_t data[50] = {};
    data[0] = 0xFF; // Wrong magic
    auto result = ContractAudioPacket::unpack(data, sizeof(data));
    assert(!result.has_value());
    std::cout << "test_contract_invalid_magic PASSED\n";
}

void test_contract_truncated() {
    uint8_t data[10] = {};
    auto result = ContractAudioPacket::unpack(data, sizeof(data));
    assert(!result.has_value());
    std::cout << "test_contract_truncated PASSED\n";
}

void test_silence_detection() {
    ContractAudioPacket pkt;
    pkt.meta.vad_flag = 0;
    assert(pkt.is_silence());

    pkt.meta.vad_flag = 1;
    assert(!pkt.is_silence());

    std::cout << "test_silence_detection PASSED\n";
}

int main() {
    test_contract_packet_roundtrip();
    test_contract_invalid_magic();
    test_contract_truncated();
    test_silence_detection();
    std::cout << "All tests PASSED\n";
    return 0;
}
