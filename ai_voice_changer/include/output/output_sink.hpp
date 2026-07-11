// output/output_sink.hpp
// Abstract sink for the 48kHz post-processed audio stream.
#pragma once

#include <cstddef>
#include <span>

namespace mozart {

class OutputSink {
public:
    virtual ~OutputSink() = default;

    // Write one output frame (mono float32, [-1,1]).
    // Samples count is flexible (one 20ms frame = 960 samples at 48kHz).
    virtual void write(std::span<const float> pcm) = 0;

    // Human-readable name for logging/status.
    virtual const char* name() const = 0;
};

} // namespace mozart