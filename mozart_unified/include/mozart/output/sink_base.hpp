#pragma once
#include <mozart/core/frame_meta.hpp>

namespace mozart {

// Abstract base for audio output sinks
class SinkBase {
public:
    virtual ~SinkBase() = default;

    // Write output frame (48kHz, 960 samples)
    virtual void write(const OutputFrameBuf& frame) = 0;

    // Get sink name for logging
    virtual const char* name() const = 0;
};

}  // namespace mozart
