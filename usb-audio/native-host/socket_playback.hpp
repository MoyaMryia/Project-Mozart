#pragma once

#include "audio_pcm.hpp"
#include "runtime.hpp"

#include <memory>
#include <string>

namespace mozart_host {

// Playback endpoint backed by a child process. The child owns the PipeWire
// playback stream; this process only sends PCM blocks and receives acks.
class SocketPlayback final : public AudioPcm {
public:
    SocketPlayback(unsigned block, unsigned periods, unsigned target_frames,
                   std::string target, Failure& failure);
    ~SocketPlayback() override;

    SocketPlayback(const SocketPlayback&) = delete;
    SocketPlayback& operator=(const SocketPlayback&) = delete;

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
