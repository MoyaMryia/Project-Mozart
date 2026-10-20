#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>

#include <chrono>
#include <atomic>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <thread>
#include <vector>

#include "mozart/frame_meta.h"
#include "mozart/audio_io.h"
#include "mozart/udp_stream.hpp"
#include "rvc/audio_worker.hpp"
#include "rvc/pipeline.hpp"
#include "state/realtime_rvc_worker.hpp"

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
    const auto require = [](bool value, const char* message) {
        if (!value) throw std::runtime_error(message);
    };
    struct Socket {
        int fd = ::socket(AF_INET, SOCK_DGRAM, 0);
        ~Socket() { if (fd >= 0) ::close(fd); }
    } first, second, reservation;
    try {
        require(first.fd >= 0 && second.fd >= 0 && reservation.fd >= 0, "socket creation failed");
        sockaddr_in address{};
        address.sin_family = AF_INET;
        ::inet_pton(AF_INET, "127.0.0.1", &address.sin_addr);
        require(::bind(reservation.fd, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0,
                "port reservation failed");
        socklen_t size = sizeof(address);
        require(::getsockname(reservation.fd, reinterpret_cast<sockaddr*>(&address), &size) == 0,
                "port query failed");
        const uint16_t port = ntohs(address.sin_port);
        ::close(reservation.fd);
        reservation.fd = -1;
        const timeval timeout{0, 200000};
        for (int fd : {first.fd, second.fd}) {
            require(::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) == 0,
                    "receive timeout failed");
        }
        const auto send = [&](int fd, uint32_t index, float sample) {
            auto packet = make_input_packet(index);
            for (uint32_t i = 0; i < MOZART_INPUT_SAMPLES; ++i) {
                std::memcpy(packet.data() + MOZART_PACKET_HEADER_SIZE + i * sizeof(float), &sample, sizeof(sample));
            }
            require(::sendto(fd, packet.data(), packet.size(), 0,
                             reinterpret_cast<sockaddr*>(&address), sizeof(address)) == static_cast<ssize_t>(packet.size()),
                    "input send failed");
        };
        const auto receive = [&](int fd, uint32_t index, float sample) {
            unsigned char buffer[4096];
            const auto bytes = ::recv(fd, buffer, sizeof(buffer), 0);
            require(bytes == MOZART_PACKET_HEADER_SIZE + MOZART_OUTPUT_SAMPLES * sizeof(float),
                    "output packet missing or malformed");
            uint32_t returned_index = 0;
            float returned_sample = 0;
            std::memcpy(&returned_index, buffer + 12, sizeof(returned_index));
            std::memcpy(&returned_sample, buffer + MOZART_PACKET_HEADER_SIZE, sizeof(returned_sample));
            require(returned_index == index, "output belongs to another input frame");
            require(std::abs(returned_sample - sample) < 1e-6f, "output contains another client's samples");
        };
        const auto no_output = [&](int fd) {
            unsigned char buffer[4096];
            require(::recv(fd, buffer, sizeof(buffer), 0) < 0 && (errno == EAGAIN || errno == EWOULDBLOCK),
                    "a rejected client caused an output packet");
        };
        const auto destroy = [](void* value) { mozart_io_destroy_stream(value); };
        std::unique_ptr<void, decltype(destroy)> stream(
            mozart_io_create_udp_stream("127.0.0.1", port, MOZART_IO_DIR_CAPTURE), destroy);
        require(stream != nullptr, "UDP stream creation failed");
        const auto open = [&] {
            require(mozart_io_open_stream(stream.get(), MOZART_INPUT_SAMPLE_RATE, MOZART_INPUT_FRAME_MS, 16),
                    "UDP stream open failed");
        };
        auto* udp = static_cast<mozart::UdpStream*>(stream.get());
        const auto rejected = [&](uint64_t count) {
            const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
            while (udp->rejected_clients() < count && std::chrono::steady_clock::now() < deadline) {
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
            }
            require(udp->rejected_clients() == count, "foreign client was not rejected");
        };
        rvc::MockRVCPipeline pipeline;
        rvc::AudioWorker::Config config;
        config.host = "127.0.0.1";
        config.port = port;
        open();
        {
            rvc::AudioWorker worker(stream.get(), pipeline, config);
            worker.start();
            for (uint32_t i = 0; i < 3; ++i) send(first.fd, i, .1f);
            for (uint32_t i = 0; i < 3; ++i) receive(first.fd, i, .1f);
            sockaddr_in owner{};
            size = sizeof(owner);
            require(::getsockname(first.fd, reinterpret_cast<sockaddr*>(&owner), &size) == 0, "client port query failed");
            require(udp->client_endpoint() == std::make_pair(std::string("127.0.0.1"), ntohs(owner.sin_port)),
                    "reported client is incorrect");
            send(second.fd, 99, .9f);
            rejected(1);
            no_output(first.fd);
            no_output(second.fd);
            require(worker.get_vad_stats().frame_count == 3, "foreign input entered inference");
            send(first.fd, 3, .4f);
            receive(first.fd, 3, .4f);
            worker.stop();
        }
        require(udp->client_endpoint() == std::make_pair(std::string(), uint16_t{0}), "closed session retained its client");
        open();
        {
            rvc::AudioWorker restarted(stream.get(), pipeline, config);
            restarted.start();
            send(second.fd, 0, -.4f);
            receive(second.fd, 0, -.4f);
            no_output(first.fd);
            send(first.fd, 99, .9f);
            rejected(2);
            no_output(first.fd);
            no_output(second.fd);
            require(restarted.get_vad_stats().frame_count == 1, "old client entered the new session");
            restarted.stop();
        }
        require(udp->packets_sent() == 5 && udp->packets_dropped() == 2, "transport counters differ from observed packets");
        // 将关闭请求放在推理期间，检查旧输出和会话归属一起被回收。
        class DelayedPipeline : public rvc::MockRVCPipeline {
        public:
            std::atomic<bool> started{false}, completed{false};
            std::vector<float> process(const std::vector<float>& input) override {
                started = true;
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
                auto output = MockRVCPipeline::process(input);
                completed = true;
                return output;
            }
        } delayed;
        rvc::RealtimeRvcWorker::Config session_config;
        session_config.host = "127.0.0.1";
        session_config.port = port;
        rvc::RealtimeRvcWorker session(delayed, session_config);
        session.start();
        send(first.fd, 10, .8f);
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
        while (!delayed.started && std::chrono::steady_clock::now() < deadline) {
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
        require(delayed.started, "delayed inference did not start");
        session.stop();
        require(delayed.completed, "session closed before its old inference ended");
        no_output(first.fd);
        session.start();
        send(second.fd, 0, -.7f);
        receive(second.fd, 0, -.7f);
        no_output(first.fd);
        const auto restarted_stats = session.stream_stats();
        require(restarted_stats.packets_received == 1 && restarted_stats.packets_sent == 1
                && restarted_stats.peer_port != 0, "new session retained old transport state");
        session.stop();
        std::cout << "test_udp_loopback PASSED: owner isolation, explicit restart, PCM ownership, in-flight teardown\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
