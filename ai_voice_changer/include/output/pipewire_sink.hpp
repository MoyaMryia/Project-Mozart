// output/pipewire_sink.hpp
// Registers a PipeWire virtual source so other applications can select the
// converted voice as if it were a microphone. Requires MOZART_ENABLE_PIPEWIRE.
#pragma once

#include "output/output_sink.hpp"

#include <memory>
#include <string>

namespace mozart {

class PipeWireSink final : public OutputSink {
public:
    // sample_rate expected 48000, channels 1.
    explicit PipeWireSink(const std::string& source_name,
                          int sample_rate = 48000,
                          int channels = 1);
    ~PipeWireSink() override;

    PipeWireSink(const PipeWireSink&) = delete;
    PipeWireSink& operator=(const PipeWireSink&) = delete;

    void write(std::span<const float> pcm) override;

    const char* name() const override { return "pipewire"; }

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace mozart