#include <mozart/source/udp_source.hpp>
#include <mozart/output/udp_sink.hpp>
#include <mozart/rvc/rvc_pipeline.hpp>
#include <mozart/core/contract_packet.hpp>
#include <iostream>
#include <thread>
#include <chrono>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>

using namespace mozart;

int main() {
    const uint16_t SERVER_PORT = 19000;
    const uint16_t CLIENT_PORT = 19001;

    // Start UDP source (server)
    UdpSource::Config src_cfg;
    src_cfg.port = SERVER_PORT;
    src_cfg.buffer_frames = 10;
    UdpSource source(src_cfg);

    // Start UDP sink (client sending to server)
    UdpSink::Config sink_cfg;
    sink_cfg.dest_addr = "127.0.0.1";
    sink_cfg.dest_port = SERVER_PORT;
    UdpSink sink(sink_cfg);

    // Create mock pipeline
    MockRVCPipeline pipeline;

    // Give server time to bind
    std::this_thread::sleep_for(std::chrono::milliseconds(100));

    // Create client socket
    int client_sock = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in server_addr{};
    server_addr.sin_family = AF_INET;
    server_addr.sin_port = htons(SERVER_PORT);
    inet_pton(AF_INET, "127.0.0.1", &server_addr.sin_addr);

    // Send 3 frames
    for (int i = 0; i < 3; ++i) {
        ContractPacket pkt;
        pkt.meta.pts_ns = i * 20000000ULL;  // 20ms intervals
        pkt.meta.frame_idx = i;
        pkt.meta.vad_flag = 1;
        pkt.meta.energy_db = 128;
        pkt.meta.conf = 255;
        pkt.meta.segment_id = 1;

        // Generate 440Hz sine wave at 16kHz
        pkt.samples.resize(320);
        for (size_t j = 0; j < 320; ++j) {
            pkt.samples[j] = std::sin(2.0 * M_PI * 440.0 * (i * 320 + j) / 16000.0);
        }

        auto data = pkt.pack();
        sendto(client_sock, data.data(), data.size(), 0,
               reinterpret_cast<struct sockaddr*>(&server_addr), sizeof(server_addr));
    }

    std::cout << "Sent 3 frames to UDP source\n";

    // Receive and process frames
    for (int i = 0; i < 3; ++i) {
        FrameBuf in_frame;
        bool got = source.poll(in_frame, 3000000);  // 3s timeout
        if (!got) {
            std::cerr << "Failed to receive frame " << i << "\n";
            return 1;
        }

        assert(in_frame.meta.frame_idx == static_cast<uint32_t>(i));

        // Process through pipeline
        OutputFrameBuf out_frame;
        pipeline.process(in_frame, out_frame);

        // Verify output is 48kHz (960 samples)
        assert(out_frame.samples.size() == 960);

        std::cout << "[PASS] Frame " << i << ": received 320 samples, output 960 samples\n";
    }

    close(client_sock);
    std::cout << "All UDP loopback tests passed!\n";
    return 0;
}
