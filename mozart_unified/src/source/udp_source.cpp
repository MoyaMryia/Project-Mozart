#include <mozart/source/udp_source.hpp>
#include <mozart/core/contract_packet.hpp>
#include <mozart/utils/logging.hpp>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <cstring>

namespace mozart {

UdpSource::UdpSource(const Config& cfg) : cfg_(cfg) {
    sock_ = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock_ < 0) {
        throw std::runtime_error("Failed to create UDP socket");
    }

    struct sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons(cfg_.port);

    if (bind(sock_, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) < 0) {
        close(sock_);
        throw std::runtime_error("Failed to bind UDP socket to port " + std::to_string(cfg_.port));
    }

    running_ = true;
    recv_thread_ = std::thread(&UdpSource::recv_loop, this);

    MOZART_LOG_INFO("UDP source listening on port {}", cfg_.port);
}

UdpSource::~UdpSource() {
    running_ = false;
    if (recv_thread_.joinable()) {
        recv_thread_.join();
    }
    if (sock_ >= 0) {
        close(sock_);
    }
}

void UdpSource::recv_loop() {
    std::vector<uint8_t> buf(4096);
    struct sockaddr_in client_addr{};
    socklen_t addr_len = sizeof(client_addr);

    while (running_) {
        ssize_t n = recvfrom(sock_, buf.data(), buf.size(), 0,
                             reinterpret_cast<struct sockaddr*>(&client_addr), &addr_len);
        if (n <= 0) continue;

        auto pkt = ContractPacket::unpack(buf.data(), n);
        if (!pkt) {
            MOZART_LOG_WARN("Received invalid contract packet");
            continue;
        }

        // Convert to FrameBuf
        FrameBuf frame;
        frame.meta = pkt->meta;
        size_t copy_len = std::min(pkt->samples.size(), frame.samples.size());
        std::memcpy(frame.samples.data(), pkt->samples.data(), copy_len * sizeof(float));

        {
            std::lock_guard<std::mutex> lock(mtx_);
            if (queue_.size() >= cfg_.buffer_frames) {
                queue_.pop();  // Drop oldest if queue full
            }
            queue_.push(std::move(frame));
        }
        cv_.notify_one();
    }
}

bool UdpSource::poll(FrameBuf& out_frame, int timeout_us) {
    std::unique_lock<std::mutex> lock(mtx_);
    if (cv_.wait_for(lock, std::chrono::microseconds(timeout_us),
                     [this] { return !queue_.empty(); })) {
        out_frame = std::move(queue_.front());
        queue_.pop();
        return true;
    }
    return false;
}

std::string UdpSource::client_addr() const {
    // TODO: Track client address from recvfrom
    return "unknown";
}

}  // namespace mozart
