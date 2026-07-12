#include <mozart/source/mock_source.hpp>
#include <cmath>
#include <thread>

namespace mozart {

MockSource::MockSource(float frequency)
    : frequency_(frequency), start_time_(std::chrono::steady_clock::now()) {}

bool MockSource::poll(FrameBuf& out_frame, int timeout_us) {
    // Simulate real-time: sleep for 20ms (one frame duration)
    std::this_thread::sleep_for(std::chrono::microseconds(timeout_us));

    // Generate 440Hz sine wave
    constexpr double kSampleRate = 16000.0;
    constexpr size_t kSamples = 320;
    constexpr double kTwoPi = 2.0 * M_PI;

    for (size_t i = 0; i < kSamples; ++i) {
        out_frame.samples[i] = static_cast<float>(std::sin(phase_ * kTwoPi));
        phase_ += frequency_ / kSampleRate;
        if (phase_ >= 1.0) phase_ -= 1.0;
    }

    // Fill metadata
    auto now = std::chrono::steady_clock::now();
    auto elapsed = std::chrono::duration_cast<std::chrono::nanoseconds>(now - start_time_);
    out_frame.meta.pts_ns = elapsed.count();
    out_frame.meta.frame_idx = frame_idx_++;
    out_frame.meta.vad_flag = 1;  // Always voice for mock
    out_frame.meta.energy_db = 128;
    out_frame.meta.conf = 255;
    out_frame.meta.segment_id = 1;

    return true;
}

}  // namespace mozart
