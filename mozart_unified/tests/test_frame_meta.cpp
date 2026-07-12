#include <mozart/core/frame_meta.hpp>
#include <cassert>
#include <cstring>
#include <iostream>

using namespace mozart;

int main() {
    // Test 1: Size check
    static_assert(sizeof(FrameMeta) == 16, "FrameMeta must be 16 bytes");
    std::cout << "[PASS] FrameMeta size = " << sizeof(FrameMeta) << " bytes\n";

    // Test 2: Alignment check
    static_assert(alignof(FrameMeta) == 1, "FrameMeta must be packed");
    std::cout << "[PASS] FrameMeta alignment = " << alignof(FrameMeta) << "\n";

    // Test 3: Field offsets
    FrameMeta meta{};
    meta.pts_ns = 0x0102030405060708ULL;
    meta.frame_idx = 0x0A0B0C0D;
    meta.vad_flag = 0x11;
    meta.energy_db = 0x22;
    meta.conf = 0x33;
    meta.segment_id = 0x44;

    uint8_t buf[16];
    std::memcpy(buf, &meta, sizeof(meta));

    // Verify little-endian layout
    assert(buf[0] == 0x08);  // pts_ns LSB
    assert(buf[7] == 0x01);  // pts_ns MSB
    assert(buf[8] == 0x0D);  // frame_idx LSB
    assert(buf[11] == 0x0A); // frame_idx MSB
    assert(buf[12] == 0x11); // vad_flag
    assert(buf[13] == 0x22); // energy_db
    assert(buf[14] == 0x33); // conf
    assert(buf[15] == 0x44); // segment_id

    std::cout << "[PASS] Field offsets correct\n";

    // Test 4: Round-trip
    FrameMeta meta2 = frame_meta_from_bytes(buf);
    assert(meta2.pts_ns == meta.pts_ns);
    assert(meta2.frame_idx == meta.frame_idx);
    assert(meta2.vad_flag == meta.vad_flag);
    assert(meta2.energy_db == meta.energy_db);
    assert(meta2.conf == meta.conf);
    assert(meta2.segment_id == meta.segment_id);

    std::cout << "[PASS] Round-trip serialization\n";

    std::cout << "All frame_meta tests passed!\n";
    return 0;
}
