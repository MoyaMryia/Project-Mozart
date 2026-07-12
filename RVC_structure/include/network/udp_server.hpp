#pragma once

#include <cstdint>
#include <functional>
#include <thread>
#include <atomic>
#include <mutex>
#include <deque>
#include <condition_variable>
#include <chrono>

#include "network/packet.hpp"

namespace rvc {

// ──────────────────────────────────────────────────────────
// Inference callback signature
// Input: float32 samples @ input_sample_rate
// Output: float32 samples @ output_sample_rate
// ──────────────────────────────────────────────────────────
using InferenceCallback = std::function<std::vector<float>(const std::vector<float>&)>;

// ──────────────────────────────────────────────────────────
// UDP server for contract-stream voice conversion
// Receives ContractAudioPacket, runs inference callback, sends back result
// ──────────────────────────────────────────────────────────
class UdpAudioServer {
public:
    UdpAudioServer(
        const std::string& host, uint16_t port,
        uint32_t input_sample_rate,
        uint32_t output_sample_rate,
        uint32_t frame_duration_ms,
        InferenceCallback callback,
        bool skip_silence = true,
        uint32_t frames_per_inference = 1
    );

    ~UdpAudioServer();

    void start();
    void stop();

    struct LatencyStats {
        uint64_t count = 0;
        double avg_ms = 0.0;
        double max_ms = 0.0;
    };

    struct BypassStats {
        uint64_t inference_count = 0;
        uint64_t bypass_count = 0;
    };

    LatencyStats get_latency_stats() const;
    BypassStats get_bypass_stats() const;

    // Config accessors
    const std::string& host() const { return host_; }
    uint16_t port() const { return port_; }
    uint32_t input_sample_rate() const { return input_sample_rate_; }
    uint32_t output_sample_rate() const { return output_sample_rate_; }
    uint32_t frame_duration_ms() const { return frame_duration_ms_; }
    size_t input_samples_per_frame() const { return input_samples_per_frame_; }
    size_t output_samples_per_frame() const { return output_samples_per_frame_; }

private:
    std::string host_;
    uint16_t port_;
    uint32_t input_sample_rate_;
    uint32_t output_sample_rate_;
    uint32_t frame_duration_ms_;
    InferenceCallback inference_callback_;
    bool skip_silence_;
    uint32_t frames_per_inference_;

    size_t input_samples_per_frame_;
    size_t output_samples_per_frame_;

    int sock_fd_ = -1;
    struct sockaddr_in client_addr_;
    socklen_t client_addr_len_ = sizeof(client_addr_);
    std::atomic<bool> client_known_{false};

    std::atomic<bool> running_{false};
    std::thread recv_thread_;
    std::thread process_thread_;
    std::thread send_thread_;

    std::mutex input_mutex_;
    std::deque<ContractAudioPacket> input_buffer_;
    std::condition_variable input_cv_;

    std::mutex output_mutex_;
    std::deque<ContractAudioPacket> output_queue_;
    std::condition_variable output_cv_;

    mutable std::mutex stats_mutex_;
    std::deque<double> latency_stats_;
    uint64_t inference_count_ = 0;
    uint64_t bypass_count_ = 0;

    void receive_loop();
    void process_loop();
    void send_loop();

    std::vector<ContractAudioPacket> collect_frames();
    ContractAudioPacket process_frame(const ContractAudioPacket& in_pkt);
    std::vector<float> ensure_frame_length(const std::vector<float>& samples);
};

} // namespace rvc
