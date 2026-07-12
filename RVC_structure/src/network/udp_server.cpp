#include "network/udp_server.hpp"
#include <spdlog/spdlog.h>
#include <cstring>

namespace rvc {

UdpAudioServer::UdpAudioServer(
    const std::string& host, uint16_t port,
    uint32_t input_sample_rate,
    uint32_t output_sample_rate,
    uint32_t frame_duration_ms,
    InferenceCallback callback,
    bool skip_silence,
    uint32_t frames_per_inference
)
    : host_(host), port_(port)
    , input_sample_rate_(input_sample_rate)
    , output_sample_rate_(output_sample_rate)
    , frame_duration_ms_(frame_duration_ms)
    , inference_callback_(std::move(callback))
    , skip_silence_(skip_silence)
    , frames_per_inference_(frames_per_inference)
    , input_samples_per_frame_(input_sample_rate * frame_duration_ms / 1000)
    , output_samples_per_frame_(output_sample_rate * frame_duration_ms / 1000)
{
    std::memset(&client_addr_, 0, sizeof(client_addr_));
}

UdpAudioServer::~UdpAudioServer() {
    stop();
}

void UdpAudioServer::start() {
    sock_fd_ = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock_fd_ < 0) {
        spdlog::error("Failed to create UDP socket");
        throw std::runtime_error("socket creation failed");
    }

    // Set receive timeout to 100ms for graceful shutdown
    struct timeval tv;
    tv.tv_sec = 0;
    tv.tv_usec = 100000;
    setsockopt(sock_fd_, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    struct sockaddr_in addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port_);
    inet_pton(AF_INET, host_.c_str(), &addr.sin_addr);

    if (bind(sock_fd_, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) < 0) {
        spdlog::error("Failed to bind UDP socket to {}:{}", host_, port_);
        socket_close(sock_fd_);
        throw std::runtime_error("socket bind failed");
    }

    running_ = true;
    recv_thread_ = std::thread(&UdpAudioServer::receive_loop, this);
    process_thread_ = std::thread(&UdpAudioServer::process_loop, this);
    send_thread_ = std::thread(&UdpAudioServer::send_loop, this);

    spdlog::info("UDP contract server listening on {}:{} ({}Hz -> {}Hz, {}ms frame)",
                 host_, port_, input_sample_rate_, output_sample_rate_, frame_duration_ms_);
}

void UdpAudioServer::stop() {
    running_ = false;
    input_cv_.notify_all();
    output_cv_.notify_all();

    if (sock_fd_ >= 0) {
        socket_close(sock_fd_);
        sock_fd_ = -1;
    }

    if (recv_thread_.joinable()) recv_thread_.join();
    if (process_thread_.joinable()) process_thread_.join();
    if (send_thread_.joinable()) send_thread_.join();

    spdlog::info("UDP server stopped. Inferences={}, Bypassed={}", inference_count_, bypass_count_);
}

void UdpAudioServer::receive_loop() {
    std::vector<uint8_t> buffer(8192);

    while (running_) {
        struct sockaddr_in src_addr;
        socklen_t src_len = sizeof(src_addr);

        ssize_t n = recvfrom(sock_fd_, buffer.data(), buffer.size(), 0,
                             reinterpret_cast<struct sockaddr*>(&src_addr), &src_len);

        if (n < 0) {
#ifdef _WIN32
            int err = WSAGetLastError();
            if (err == WSAETIMEDOUT) continue;
            if (!running_) break;
#else
            if (errno == EAGAIN || errno == EWOULDBLOCK) continue;
            if (!running_) break;
#endif
            spdlog::warn("UDP receive error: {}", n);
            continue;
        }

        if (!client_known_.load()) {
            client_addr_ = src_addr;
            client_addr_len_ = src_len;
            client_known_.store(true);
        }

        auto pkt = ContractAudioPacket::unpack(
            buffer.data(), static_cast<size_t>(n),
            input_samples_per_frame_
        );

        if (!pkt.has_value()) {
            spdlog::debug("Received non-contract packet ({} bytes)", n);
            continue;
        }

        {
            std::lock_guard<std::mutex> lock(input_mutex_);
            input_buffer_.push_back(std::move(pkt.value()));
        }
        input_cv_.notify_one();
    }
}

void UdpAudioServer::process_loop() {
    while (running_) {
        auto frames = collect_frames();
        if (frames.empty()) {
            std::unique_lock<std::mutex> lock(input_mutex_);
            input_cv_.wait_for(lock, std::chrono::milliseconds(1),
                [this] { return !input_buffer_.empty() || !running_; });
            continue;
        }

        auto start = std::chrono::steady_clock::now();

        for (const auto& in_pkt : frames) {
            auto out_pkt = process_frame(in_pkt);
            {
                std::lock_guard<std::mutex> lock(output_mutex_);
                output_queue_.push_back(std::move(out_pkt));
            }
            output_cv_.notify_one();
        }

        auto elapsed = std::chrono::steady_clock::now() - start;
        double elapsed_ms = std::chrono::duration<double, std::milli>(elapsed).count();

        {
            std::lock_guard<std::mutex> lock(stats_mutex_);
            latency_stats_.push_back(elapsed_ms);
            if (latency_stats_.size() > 1000) latency_stats_.pop_front();
        }
    }
}

ContractAudioPacket UdpAudioServer::process_frame(const ContractAudioPacket& in_pkt) {
    if (skip_silence_ && in_pkt.is_silence()) {
        bypass_count_++;
        return ContractAudioPacket::from_samples(
            in_pkt.meta.pts_ns, in_pkt.meta.frame_idx,
            std::vector<float>(output_samples_per_frame_, 0.0f),
            in_pkt.meta.vad_flag, in_pkt.meta.energy_db,
            in_pkt.meta.conf, in_pkt.meta.segment_id
        );
    }

    auto converted = inference_callback_(in_pkt.samples);
    converted = ensure_frame_length(converted);

    inference_count_++;
    return ContractAudioPacket::from_samples(
        in_pkt.meta.pts_ns, in_pkt.meta.frame_idx,
        converted,
        in_pkt.meta.vad_flag, in_pkt.meta.energy_db,
        in_pkt.meta.conf, in_pkt.meta.segment_id
    );
}

std::vector<float> UdpAudioServer::ensure_frame_length(const std::vector<float>& samples) {
    if (samples.size() == output_samples_per_frame_) {
        return samples;
    }
    if (samples.size() < output_samples_per_frame_) {
        std::vector<float> result(output_samples_per_frame_, 0.0f);
        std::copy(samples.begin(), samples.end(), result.begin());
        return result;
    }
    return std::vector<float>(samples.begin(), samples.begin() + output_samples_per_frame_);
}

std::vector<ContractAudioPacket> UdpAudioServer::collect_frames() {
    std::lock_guard<std::mutex> lock(input_mutex_);
    if (input_buffer_.size() < frames_per_inference_) {
        return {};
    }
    std::vector<ContractAudioPacket> result;
    for (uint32_t i = 0; i < frames_per_inference_ && !input_buffer_.empty(); ++i) {
        result.push_back(std::move(input_buffer_.front()));
        input_buffer_.pop_front();
    }
    return result;
}

void UdpAudioServer::send_loop() {
    while (running_) {
        ContractAudioPacket pkt;
        {
            std::unique_lock<std::mutex> lock(output_mutex_);
            if (output_queue_.empty()) {
                output_cv_.wait_for(lock, std::chrono::milliseconds(1),
                    [this] { return !output_queue_.empty() || !running_; });
                if (output_queue_.empty()) continue;
            }
            pkt = std::move(output_queue_.front());
            output_queue_.pop_front();
        }

        if (!client_known_.load()) continue;

        auto data = pkt.pack();
        ssize_t sent = sendto(sock_fd_, data.data(), data.size(), 0,
                              reinterpret_cast<struct sockaddr*>(&client_addr_),
                              client_addr_len_);
        if (sent < 0) {
            spdlog::warn("UDP send error");
        }
    }
}

UdpAudioServer::LatencyStats UdpAudioServer::get_latency_stats() const {
    std::lock_guard<std::mutex> lock(stats_mutex_);
    LatencyStats stats{};
    if (latency_stats_.empty()) return stats;

    stats.count = latency_stats_.size();
    double sum = 0.0;
    stats.max_ms = 0.0;
    for (double v : latency_stats_) {
        sum += v;
        if (v > stats.max_ms) stats.max_ms = v;
    }
    stats.avg_ms = sum / stats.count;
    return stats;
}

UdpAudioServer::BypassStats UdpAudioServer::get_bypass_stats() const {
    std::lock_guard<std::mutex> lock(stats_mutex_);
    return {inference_count_, bypass_count_};
}

} // namespace rvc
