#include <iostream>
#include <thread>
#include <chrono>
#include <cmath>
#include <cstring>
#include "network/udp_server.hpp"
#include "network/packet.hpp"
#include "rvc/pipeline.hpp"

using namespace rvc;

// Simple echo-ish callback: attenuate slightly to verify activity
std::vector<float> mock_inference(const std::vector<float>& audio) {
    std::vector<float> out;
    out.reserve(audio.size() * 3); // 16k -> 48k upsample
    for (float s : audio) {
        float attenuated = s * 0.95f;
        out.push_back(attenuated);
        out.push_back(attenuated);
        out.push_back(attenuated);
    }
    return out;
}

int main() {
    constexpr uint16_t SERVER_PORT = 19000;
    constexpr uint16_t CLIENT_PORT = 19001;
    constexpr uint32_t INPUT_SR = 16000;
    constexpr uint32_t OUTPUT_SR = 48000;
    constexpr uint32_t FRAME_MS = 20;

    // Start server
    UdpAudioServer server("127.0.0.1", SERVER_PORT,
                          INPUT_SR, OUTPUT_SR, FRAME_MS,
                          mock_inference, true, 1);
    server.start();
    std::this_thread::sleep_for(std::chrono::milliseconds(200));

    // Create client socket
    int client_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (client_fd < 0) {
        std::cerr << "Failed to create client socket\n";
        return 1;
    }

    struct sockaddr_in client_addr;
    std::memset(&client_addr, 0, sizeof(client_addr));
    client_addr.sin_family = AF_INET;
    client_addr.sin_port = htons(CLIENT_PORT);
    inet_pton(AF_INET, "127.0.0.1", &client_addr.sin_addr);
    bind(client_fd, reinterpret_cast<struct sockaddr*>(&client_addr), sizeof(client_addr));

    struct sockaddr_in server_addr;
    std::memset(&server_addr, 0, sizeof(server_addr));
    server_addr.sin_family = AF_INET;
    server_addr.sin_port = htons(SERVER_PORT);
    inet_pton(AF_INET, "127.0.0.1", &server_addr.sin_addr);

    // Send 3 frames to the server
    std::vector<ContractAudioPacket> received;
    for (uint32_t i = 0; i < 3; ++i) {
        std::vector<float> samples(INPUT_SR * FRAME_MS / 1000);
        for (size_t j = 0; j < samples.size(); ++j) {
            samples[j] = std::sin(2.0f * 3.14159f * 440.0f * static_cast<float>(j) / INPUT_SR) * 0.5f;
        }

        auto pkt = ContractAudioPacket::from_samples(
            i * 20000000ULL, i, samples, 1, 128, 255, 1
        );

        auto data = pkt.pack();
        sendto(client_fd, data.data(), data.size(), 0,
               reinterpret_cast<struct sockaddr*>(&server_addr), sizeof(server_addr));
    }

    // Receive responses
    auto start = std::chrono::steady_clock::now();
    while (received.size() < 3) {
        uint8_t buf[8192];
        ssize_t n = recvfrom(client_fd, buf, sizeof(buf), 0, nullptr, nullptr);
        if (n > 0) {
            auto pkt = ContractAudioPacket::unpack(buf, static_cast<size_t>(n), OUTPUT_SR * FRAME_MS / 1000);
            if (pkt) {
                received.push_back(std::move(*pkt));
            }
        }

        auto elapsed = std::chrono::steady_clock::now() - start;
        if (elapsed > std::chrono::seconds(3)) break;
    }

    socket_close(client_fd);
    server.stop();

    if (received.size() == 3) {
        std::cout << "test_udp_loopback PASSED: received " << received.size()
                  << " packets, each with " << received[0].samples.size()
                  << " samples @ 48kHz\n";
        return 0;
    } else {
        std::cerr << "test_udp_loopback FAILED: expected 3 packets, got "
                  << received.size() << "\n";
        return 1;
    }
}
