#pragma once
#include "sink_base.hpp"
#include <string>
#include <atomic>

namespace mozart {

// UDP sink that sends output frames back to client
class UdpSink : public SinkBase {
public:
    struct Config {
        std::string dest_addr = "127.0.0.1";
        uint16_t dest_port = 18001;
    };

    explicit UdpSink(const Config& cfg);
    ~UdpSink();

    void write(const OutputFrameBuf& frame) override;
    const char* name() const override { return "udp"; }

    // Update destination address (e.g., after learning client address from UdpSource)
    void set_dest(const std::string& addr, uint16_t port);

private:
    Config cfg_;
    int sock_ = -1;
    uint64_t frame_count_ = 0;
};

}  // namespace mozart
