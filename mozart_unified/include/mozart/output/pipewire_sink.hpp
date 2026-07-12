#pragma once
#include "sink_base.hpp"
#include <string>

namespace mozart {

// PipeWire virtual source sink
// Registers as a virtual microphone that other apps can select
class PipeWireSink : public SinkBase {
public:
    struct Config {
        std::string node_name = "mozart-virtual-source";
        std::string node_description = "Mozart AI Voice Changer";
    };

    explicit PipeWireSink(const Config& cfg = {});
    ~PipeWireSink();

    void write(const OutputFrameBuf& frame) override;
    const char* name() const override { return "pipewire"; }

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace mozart
