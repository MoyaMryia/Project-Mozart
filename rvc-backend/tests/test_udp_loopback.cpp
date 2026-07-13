#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>

#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <thread>
#include <vector>

#include "mozart/frame_meta.h"
#include "mozart/udp_stream.hpp"
#include "rvc/audio_worker.hpp"
#include "rvc/pipeline.hpp"

namespace {

void write_u32_le(unsigned char* p, uint32_t value) {
    p[0] = static_cast<unsigned char>(value);
    p[1] = static_cast<unsigned char>(value >> 8);
    p[2] = static_cast<unsigned char>(value >> 16);
    p[3] = static_cast<unsigned char>(value >> 24);
}

void write_u64_le(unsigned char* p, uint64_t value) {
    write_u32_le(p, static_cast<uint32_t>(value));
    write_u32_le(p + 4, static_cast<uint32_t>(value >> 32));
}

std::vector<unsigned char> make_input_packet(uint32_t frame_idx) {
    std::vector<unsigned char> packet(
        MOZART_PACKET_HEADER_SIZE + MOZART_INPUT_SAMPLES * sizeof(float));
    write_u32_le(packet.data(), MOZART_PACKET_MAGIC);
    write_u64_le(packet.data() + 4, frame_idx * 20000000ULL);
    write_u32_le(packet.data() + 12, frame_idx);
    packet[16] = 1;
    packet[17] = 128;
    packet[18] = 255;
    packet[19] = 1;

    for (uint32_t i = 0; i < MOZART_INPUT_SAMPLES; ++i) {
        const float sample = std::sin(2.0f * 3.14159265f * 440.0f
                                      * static_cast<float>(i)
                                      / MOZART_INPUT_SAMPLE_RATE) * 0.5f;
        std::memcpy(packet.data() + MOZART_PACKET_HEADER_SIZE
                    + i * sizeof(float), &sample, sizeof(sample));
    }
    return packet;
}

} // namespace

int main() {
    constexpr uint16_t server_port = 19000;

    rvc::MockRVCPipeline pipeline(MOZART_INPUT_SAMPLE_RATE,
                                  MOZART_OUTPUT_SAMPLE_RATE);
    mozart::UdpStream stream("127.0.0.1", server_port,
                             mozart::StreamDirection::Capture);
    mozart::StreamConfig stream_config;
    stream_config.direction = mozart::StreamDirection::Capture;
    stream_config.sample_rate = MOZART_INPUT_SAMPLE_RATE;
    if (!stream.Open(stream_config)) {
        std::cerr << "failed to open UDP stream\n";
        return 1;
    }

    rvc::AudioWorker::Config worker_config;
    worker_config.host = "127.0.0.1";
    worker_config.port = server_port;
    rvc::AudioWorker worker(stream, pipeline, worker_config);
    worker.start();

    const int client_fd = ::socket(AF_INET, SOCK_DGRAM, 0);
    if (client_fd < 0) return 1;

    timeval timeout{2, 0};
    ::setsockopt(client_fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));

    sockaddr_in server_addr{};
    server_addr.sin_family = AF_INET;
    server_addr.sin_port = htons(server_port);
    ::inet_pton(AF_INET, "127.0.0.1", &server_addr.sin_addr);

    for (uint32_t i = 0; i < 3; ++i) {
        const auto packet = make_input_packet(i);
        ::sendto(client_fd, packet.data(), packet.size(), 0,
                 reinterpret_cast<sockaddr*>(&server_addr), sizeof(server_addr));
    }

    uint32_t responses = 0;
    while (responses < 3) {
        unsigned char buffer[4096];
        const ssize_t received = ::recvfrom(client_fd, buffer, sizeof(buffer),
                                             0, nullptr, nullptr);
        if (received != static_cast<ssize_t>(
                MOZART_PACKET_HEADER_SIZE + MOZART_OUTPUT_SAMPLES * sizeof(float))) {
            break;
        }
        ++responses;
    }

    ::close(client_fd);
    worker.stop();

    if (responses != 3) {
        std::cerr << "expected 3 output packets, got " << responses << '\n';
        return 1;
    }
    std::cout << "test_udp_loopback PASSED\n";
    return 0;
}
