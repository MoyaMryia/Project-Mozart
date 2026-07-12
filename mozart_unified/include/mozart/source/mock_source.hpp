#pragma once
#include "source_base.hpp"
#include <chrono>

namespace mozart {

// Mock source generating 440Hz sine wave for testing
class MockSource : public SourceBase {
public:
    MockSource(float frequency = 440.0f);

    bool poll(FrameBuf& out_frame, int timeout_us = 100000) override;
    const char* name() const override { return "mock"; }

private:
    float frequency_;
    uint32_t frame_idx_ = 0;
    double phase_ = 0.0;
    std::chrono::steady_clock::time_point start_time_;
};

}  // namespace mozart
