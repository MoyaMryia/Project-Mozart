#pragma once

#include "audio_pcm.hpp"
#include "runtime.hpp"

#include <functional>
#include <memory>
#include <string>

namespace mozart_host {

// PipeWire virtual endpoint. Capture reads a sink monitor; playback feeds a
// virtual source. The stream is driven by the PipeWire graph, not by ALSA.
class PipeWirePcm final : public AudioPcm {
public:
    using Xrun = std::function<void(bool capture, bool overrun)>;

    PipeWirePcm(bool capture, unsigned block, unsigned periods, std::string target,
                Failure& failure, Xrun xrun);
    ~PipeWirePcm() override;

    PipeWirePcm(const PipeWirePcm&) = delete;
    PipeWirePcm& operator=(const PipeWirePcm&) = delete;

    void start() override;
    void stop_capture() override;
    void set_target(unsigned frames) override;
    int64_t delay() override;
    void wait_target() override;
    void transfer(uint8_t* data, unsigned frames) override;
    void drain() override;
    bool running() override;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace mozart_host
