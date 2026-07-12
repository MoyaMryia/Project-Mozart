#pragma once
#include <mozart/core/frame_meta.hpp>
#include <functional>

namespace mozart {

// Abstract base for audio frame sources
class SourceBase {
public:
    virtual ~SourceBase() = default;

    // Poll for next frame. Returns true if frame available, false on timeout.
    // Fills out_frame with samples and metadata.
    virtual bool poll(FrameBuf& out_frame, int timeout_us = 100000) = 0;

    // Get source name for logging
    virtual const char* name() const = 0;
};

}  // namespace mozart
