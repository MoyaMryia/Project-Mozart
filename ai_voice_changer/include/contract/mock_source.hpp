// contract/mock_source.hpp
// Synthetic contract source for local testing without the Mozart preprocessor.
// Produces 16kHz/20ms frames with configurable VAD/segment patterns.
#pragma once

#include "contract/contract_source.hpp"

#include <cstdint>

namespace mozart {

class MockSource final : public ContractSource {
public:
    // Configurable tone frequency in Hz; 0 -> silence frames (vad=0).
    explicit MockSource(float tone_hz = 440.0f, int sample_rate = 16000);

    PollResult poll(ContractFrame& out_pcm, FrameMeta& out_meta,
                    int timeout_us) override;

    const char* name() const override { return "mock"; }

    // For tests: force a segment_id change after N frames to exercise reset.
    void set_segment_change_interval(int frames);

private:
    float   tone_hz_;
    int     sample_rate_;
    double  phase_     = 0.0;
    uint32_t frame_idx_ = 0;
    uint8_t  segment_id_ = 1;
    int      seg_change_interval_ = 0;  // 0 = never change
};

} // namespace mozart