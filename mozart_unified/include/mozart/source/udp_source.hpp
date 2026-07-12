#pragma once
#include "source_base.hpp"
#include <string>
#include <atomic>
#include <thread>
#include <queue>
#include <mutex>
#include <condition_variable>

namespace mozart {

// UDP source receiving ContractPacket from network
class UdpSource : public SourceBase {
public:
    struct Config {
        uint16_t port = 18000;
        size_t buffer_frames = 10;  // Internal queue size
    };

    explicit UdpSource(const Config& cfg);
    ~UdpSource();

    bool poll(FrameBuf& out_frame, int timeout_us = 100000) override;
    const char* name() const override { return "udp"; }

    // Get client address once learned
    std::string client_addr() const;

private:
    Config cfg_;
    int sock_ = -1;
    std::atomic<bool> running_{false};
    std::thread recv_thread_;

    std::queue<FrameBuf> queue_;
    std::mutex mtx_;
    std::condition_variable cv_;

    void recv_loop();
};

}  // namespace mozart
