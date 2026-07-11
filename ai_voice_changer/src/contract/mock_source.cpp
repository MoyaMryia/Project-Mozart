// contract/mock_source.cpp
#include "contract/mock_source.hpp"
#include "utils/logging.hpp"

#include <chrono>
#include <cmath>
#include <thread>

namespace mozart {

MockSource::MockSource(float tone_hz, int sample_rate)
    : tone_hz_(tone_hz), sample_rate_(sample_rate) {}

void MockSource::set_segment_change_interval(int frames) {
    seg_change_interval_ = frames;
}

PollResult MockSource::poll(ContractFrame& out_pcm, FrameMeta& out_meta,
                            int /*timeout_us*/) {
    // Simulate one contract frame period to keep timing realistic for tests.
    static constexpr int kFrameUs = 20000;  // 20 ms
    std::this_thread::sleep_for(std::chrono::microseconds(kFrameUs));

    constexpr double kPi = 3.14159265358979323846;
    const double w = 2.0 * kPi * tone_hz_ / static_cast<double>(sample_rate_);

    const bool voiced = (tone_hz_ > 0.0f);
    for (std::size_t i = 0; i < kContractSamples; ++i) {
        if (voiced) {
            phase_ += w;
            if (phase_ > 2.0 * kPi) phase_ -= 2.0 * kPi;
            out_pcm[i] = static_cast<float>(0.3 * std::sin(phase_));
        } else {
            out_pcm[i] = 0.0f;
        }
    }

    out_meta.pts_ns    = static_cast<uint64_t>(frame_idx_) * 20ULL * 1000000ULL;
    out_meta.frame_idx = frame_idx_++;
    out_meta.vad_flag  = voiced ? 1 : 0;
    out_meta.energy_db = voiced ? 60 : 10;
    out_meta.conf      = 255;
    out_meta.segment_id = voiced ? segment_id_ : 0;

    if (seg_change_interval_ > 0 && voiced &&
        (frame_idx_ % seg_change_interval_) == 0) {
        ++segment_id_;
        MOZART_INFO("MockSource: segment_id -> %u", segment_id_);
    }

    return PollResult::Ok;
}

} // namespace mozart