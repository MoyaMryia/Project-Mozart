#include <mozart/output/udp_sink.hpp>
#include <mozart/core/contract_packet.hpp>
#include <mozart/utils/logging.hpp>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>

namespace mozart {

UdpSink::UdpSink(const Config& cfg) : cfg_(cfg) {
    sock_ = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock_ < 0) {
        throw std::runtime_error("Failed to create UDP socket");
    }
    MOZART_LOG_INFO("UDP sink targeting {}:{}", cfg.dest_addr, cfg.dest_port);
}

UdpSink::~UdpSink() {
    if (sock_ >= 0) {
        close(sock_);
    }
}

void UdpSink::write(const OutputFrameBuf& frame) {
    // Build contract packet
    ContractPacket pkt;
    pkt.meta.pts_ns = 0;  // TODO: Track timestamps
    pkt.meta.frame_idx = static_cast<uint32_t>(frame_count_++);
    pkt.meta.vad_flag = 1;
    pkt.meta.energy_db = 128;
    pkt.meta.conf = 255;
    pkt.meta.segment_id = 1;
    pkt.samples.assign(frame.samples.begin(), frame.samples.end());

    auto data = pkt.pack();

    // Send to destination
    struct sockaddr_in dest{};
    dest.sin_family = AF_INET;
    dest.sin_port = htons(cfg_.dest_port);
    inet_pton(AF_INET, cfg_.dest_addr.c_str(), &dest.sin_addr);

    ssize_t sent = sendto(sock_, data.data(), data.size(), 0,
                          reinterpret_cast<struct sockaddr*>(&dest), sizeof(dest));
    if (sent < 0) {
        MOZART_LOG_WARN("UDP send failed");
    }
}

void UdpSink::set_dest(const std::string& addr, uint16_t port) {
    cfg_.dest_addr = addr;
    cfg_.dest_port = port;
    MOZART_LOG_INFO("UDP sink destination updated to {}:{}", addr, port);
}

}  // namespace mozart
