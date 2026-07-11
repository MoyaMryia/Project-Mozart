// tests/frame_meta_test.cpp
#include "frame_meta.hpp"

#include <cstdio>
#include <cstring>

int main() {
    using namespace mozart;

    static_assert(sizeof(FrameMeta) == 12, "FrameMeta must be 12 bytes");
    static_assert(alignof(FrameMeta) == 1, "FrameMeta must be packed");

    FrameMeta m{};
    m.pts_ns     = 0x0102030405060708ULL;
    m.frame_idx  = 0x090A0B0Cu;
    m.vad_flag   = 0x0D;
    m.energy_db  = 0x0E;
    m.conf       = 0x0F;
    m.segment_id = 0x10;

    char buf[12];
    std::memcpy(buf, &m, sizeof(m));
    // Little-endian layout check: byte 0 = least significant byte of pts_ns.
    if (buf[0] != (char)0x08 || buf[7] != (char)0x01) {
        std::fprintf(stderr, "FAIL: pts_ns layout\n");
        return 1;
    }
    if (buf[8] != (char)0x0C || buf[11] != (char)0x10) {
        std::fprintf(stderr, "FAIL: tail layout\n");
        return 1;
    }

    FrameMeta m2 = frame_meta_from_bytes(buf);
    if (m2.pts_ns     != m.pts_ns ||
        m2.frame_idx  != m.frame_idx ||
        m2.vad_flag   != m.vad_flag ||
        m2.energy_db  != m.energy_db ||
        m2.conf       != m.conf ||
        m2.segment_id != m.segment_id) {
        std::fprintf(stderr, "FAIL: round-trip\n");
        return 1;
    }

    std::printf("frame_meta_test PASS: sizeof=%zu align=%zu\n",
                sizeof(FrameMeta), alignof(FrameMeta));
    return 0;
}