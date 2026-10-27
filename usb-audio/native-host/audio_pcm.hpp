#pragma once

#include <cstdint>

namespace mozart_host {

// Host-side audio endpoint. ALSA direct and PipeWire virtual modes implement it.
class AudioPcm {
public:
    virtual ~AudioPcm() = default;
    virtual void start() = 0;
    virtual void stop_capture() = 0;
    virtual void set_target(unsigned frames) = 0;
    virtual int64_t delay() = 0;
    virtual void wait_target() = 0;
    virtual void transfer(uint8_t* data, unsigned frames) = 0;
    virtual void drain() = 0;
    virtual bool running() = 0;
};

} // namespace mozart_host
