// output/dummy_sink.hpp
// Discarding sink for testing. Counts frames for latency/status reporting.
#pragma once

#include "output/output_sink.hpp"

#include <atomic>
#include <cstdint>

namespace mozart {

class DummySink final : public OutputSink {
public:
    void write(std::span<const float> pcm) override;

    const char* name() const override { return "dummy"; }

    uint64_t frames_written() const { return frames_; }

private:
    std::atomic<uint64_t> frames_{0};
};

} // namespace mozart