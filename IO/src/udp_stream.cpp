// udp_stream.cpp — 实时网络 UDP 契约包驱动实现
// ============================================================================
// 从 rvc-backend/network/{packet,udp_server}.cpp 抽出的纯 IO 部分：
//   - MZRT 包 pack/unpack（用 frame_meta.h 常量）
//   - Capture: receive_loop 收包 → 内部缓冲 → ReadFrame 阻塞返回 mozart_input_frame_t
//   - Playback: WriteFrame 消费 mozart_output_frame_t → pack → sendto 对端
//   - 客户端地址追踪
// 移除了原 UdpAudioServer 的推理回调、VAD bypass、批量推理、延迟统计——
// 这些属于 rvc-backend 业务层，由调用方在 ReadFrame → 推理 → WriteFrame 间处理。
#include "mozart/udp_stream.hpp"
#include "mozart/frame_meta.h"

#include <spdlog/spdlog.h>
#include <array>
#include <cstring>
#include <exception>
#include <utility>

#ifndef _WIN32
#include <arpa/inet.h>
#include <cerrno>
#include <unistd.h>
#endif

namespace mozart {

namespace {

// ---- 小端读写辅助（从 rvc-backend/common.hpp 迁移）-------------------------
inline uint32_t read_u32_le(const unsigned char* p) {
    return static_cast<uint32_t>(p[0])
         | (static_cast<uint32_t>(p[1]) << 8)
         | (static_cast<uint32_t>(p[2]) << 16)
         | (static_cast<uint32_t>(p[3]) << 24);
}
inline uint64_t read_u64_le(const unsigned char* p) {
    return static_cast<uint64_t>(read_u32_le(p))
         | (static_cast<uint64_t>(read_u32_le(p + 4)) << 32);
}
inline void write_u32_le(unsigned char* p, uint32_t v) {
    p[0] = static_cast<unsigned char>(v);
    p[1] = static_cast<unsigned char>(v >> 8);
    p[2] = static_cast<unsigned char>(v >> 16);
    p[3] = static_cast<unsigned char>(v >> 24);
}
inline void write_u64_le(unsigned char* p, uint64_t v) {
    write_u32_le(p, static_cast<uint32_t>(v));
    write_u32_le(p + 4, static_cast<uint32_t>(v >> 32));
}

inline void socket_close(socket_t fd) {
#ifdef _WIN32
    closesocket(fd);
#else
    ::close(fd);
#endif
}

inline bool socket_valid(socket_t fd) {
#ifdef _WIN32
    return fd != INVALID_SOCKET;
#else
    return fd >= 0;
#endif
}

inline socket_t invalid_socket() {
#ifdef _WIN32
    return INVALID_SOCKET;
#else
    return -1;
#endif
}

// ---- MZRT 包 unpack：字节流 → mozart_input_frame_t -------------------------
// 返回写入的 meta；失败返回 false
bool unpack_input_packet(const unsigned char* data, size_t len,
                         mozart_input_frame_t& out) {
    constexpr size_t expected = MOZART_PACKET_HEADER_SIZE
                              + MOZART_INPUT_SAMPLES * sizeof(float);
    if (len != expected) return false;
    if (read_u32_le(data) != MOZART_PACKET_MAGIC) return false;

    out.meta.pts_ns     = read_u64_le(data + 4);
    out.meta.frame_idx  = read_u32_le(data + 12);
    out.meta.vad_flag   = data[16];
    out.meta.energy_db  = data[17];
    out.meta.conf       = data[18];
    out.meta.segment_id = data[19];

    std::memcpy(out.pcm, data + MOZART_PACKET_HEADER_SIZE,
                MOZART_INPUT_SAMPLES * sizeof(float));
    return true;
}

// ---- MZRT 包 pack：mozart_output_frame_t → 字节流 -------------------------
using OutputPacket = std::array<unsigned char,
    MOZART_PACKET_HEADER_SIZE + MOZART_OUTPUT_SAMPLES * sizeof(float)>;

void pack_output_packet(const mozart_output_frame_t& in, OutputPacket& out) {
    write_u32_le(out.data(), MOZART_PACKET_MAGIC);
    write_u64_le(out.data() + 4, in.meta.pts_ns);
    write_u32_le(out.data() + 12, in.meta.frame_idx);
    out[16] = in.meta.vad_flag;
    out[17] = in.meta.energy_db;
    out[18] = in.meta.conf;
    out[19] = in.meta.segment_id;
    std::memcpy(out.data() + MOZART_PACKET_HEADER_SIZE, in.pcm,
                MOZART_OUTPUT_SAMPLES * sizeof(float));
}

} // namespace

UdpStream::UdpStream(std::string host, uint16_t port, StreamDirection direction)
    : host_(std::move(host)), port_(port), direction_(direction)
{
    std::memset(&client_addr_, 0, sizeof(client_addr_));
}

UdpStream::~UdpStream() { Close(); }

bool UdpStream::Open(const StreamConfig& config) {
    std::lock_guard<std::mutex> lifecycle_lock(lifecycle_mutex_);
    if (open_.load()) return true;
    if (config.direction != direction_
        || config.frame_duration_ms != MOZART_INPUT_FRAME_MS) {
        return false;
    }
    if (direction_ == StreamDirection::Capture
        && config.sample_rate != MOZART_INPUT_SAMPLE_RATE) {
        return false;
    }
    if (direction_ == StreamDirection::Playback
        && config.sample_rate != MOZART_OUTPUT_SAMPLE_RATE) {
        return false;
    }

#ifdef _WIN32
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return false;
    wsa_started_ = true;
#endif

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port_);
    if (::inet_pton(AF_INET, host_.c_str(), &addr.sin_addr) != 1) {
        spdlog::error("Invalid UDP IPv4 address: {}", host_);
#ifdef _WIN32
        WSACleanup();
        wsa_started_ = false;
#endif
        return false;
    }

    socket_t new_fd = ::socket(AF_INET, SOCK_DGRAM, 0);
    if (!socket_valid(new_fd)) {
        spdlog::error("UDP socket creation failed");
#ifdef _WIN32
        WSACleanup();
        wsa_started_ = false;
#endif
        return false;
    }

    // 接收超时 100ms，便于优雅关闭
#ifdef _WIN32
    DWORD timeout_ms = 100;
    const int timeout_result = ::setsockopt(
        new_fd, SOL_SOCKET, SO_RCVTIMEO,
        reinterpret_cast<const char*>(&timeout_ms), sizeof(timeout_ms));
#else
    timeval tv{0, 100000};
    const int timeout_result = ::setsockopt(
        new_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
#endif
    if (timeout_result != 0) {
        spdlog::error("Failed to configure UDP receive timeout");
        socket_close(new_fd);
#ifdef _WIN32
        WSACleanup();
        wsa_started_ = false;
#endif
        return false;
    }

    if (direction_ == StreamDirection::Capture) {
        // Capture: bind 到本地地址
        if (::bind(new_fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
            spdlog::error("UDP bind failed: {}:{}", host_, port_);
            socket_close(new_fd);
#ifdef _WIN32
            WSACleanup();
            wsa_started_ = false;
#endif
            return false;
        }

        try {
            auto new_ring = std::make_unique<SpscRing>(
                config.ring_capacity == 0 ? 16 : config.ring_capacity,
                sizeof(mozart_input_frame_t));
            {
                std::lock_guard<std::mutex> socket_lock(socket_mutex_);
                sock_fd_ = new_fd;
            }
            {
                std::lock_guard<std::mutex> input_lock(input_mutex_);
                input_ring_ = std::move(new_ring);
                open_.store(true);
            }
            {
                std::lock_guard<std::mutex> client_lock(client_mutex_);
                client_known_.store(false);
                std::memset(&client_addr_, 0, sizeof(client_addr_));
                client_addr_len_ = 0;
            }
            recv_thread_ = std::thread(&UdpStream::receive_loop, this);
        } catch (const std::exception& error) {
            spdlog::error("Failed to open UDP capture stream: {}", error.what());
            {
                std::lock_guard<std::mutex> input_lock(input_mutex_);
                open_.store(false);
                input_ring_.reset();
            }
            {
                std::lock_guard<std::mutex> socket_lock(socket_mutex_);
                sock_fd_ = invalid_socket();
            }
            socket_close(new_fd);
#ifdef _WIN32
            WSACleanup();
            wsa_started_ = false;
#endif
            return false;
        } catch (...) {
            spdlog::error("Failed to open UDP capture stream");
            {
                std::lock_guard<std::mutex> input_lock(input_mutex_);
                open_.store(false);
                input_ring_.reset();
            }
            {
                std::lock_guard<std::mutex> socket_lock(socket_mutex_);
                sock_fd_ = invalid_socket();
            }
            socket_close(new_fd);
#ifdef _WIN32
            WSACleanup();
            wsa_started_ = false;
#endif
            return false;
        }
        spdlog::info("UdpStream capture listening on {}:{}", host_, port_);
    } else {
        // Playback: 记录对端地址（不 connect，sendto 时指定）
        {
            std::lock_guard<std::mutex> socket_lock(socket_mutex_);
            sock_fd_ = new_fd;
        }
        {
            std::lock_guard<std::mutex> client_lock(client_mutex_);
            std::memcpy(&client_addr_, &addr, sizeof(addr));
            client_addr_len_ = sizeof(addr);
            client_known_.store(true);
        }
        open_.store(true);
        spdlog::info("UdpStream playback target {}:{}", host_, port_);
    }

    return true;
}

void UdpStream::Close() {
    std::lock_guard<std::mutex> lifecycle_lock(lifecycle_mutex_);
    if (!open_.load()) return;

    {
        std::lock_guard<std::mutex> input_lock(input_mutex_);
        open_.store(false);
    }
    input_cv_.notify_all();
    if (recv_thread_.joinable()) recv_thread_.join();

    {
        std::lock_guard<std::mutex> socket_lock(socket_mutex_);
        if (socket_valid(sock_fd_)) {
            socket_close(sock_fd_);
            sock_fd_ = invalid_socket();
        }
    }
    {
        std::lock_guard<std::mutex> input_lock(input_mutex_);
        input_ring_.reset();
    }
    {
        std::lock_guard<std::mutex> client_lock(client_mutex_);
        client_known_.store(false);
        std::memset(&client_addr_, 0, sizeof(client_addr_));
        client_addr_len_ = 0;
    }
#ifdef _WIN32
    if (wsa_started_) {
        WSACleanup();
        wsa_started_ = false;
    }
#endif
    spdlog::info("UdpStream closed: recv={} sent={} dropped={}",
                 packets_received_.load(), packets_sent_.load(), packets_dropped_.load());
}

void UdpStream::receive_loop() {
    std::array<unsigned char, 8192> buf{};
    while (open_.load()) {
        sockaddr_storage src{};
        socket_len_t src_len = sizeof(src);
        const auto n = ::recvfrom(sock_fd_, reinterpret_cast<char*>(buf.data()),
                                  static_cast<int>(buf.size()), 0,
                                  reinterpret_cast<sockaddr*>(&src), &src_len);
        if (n < 0) {
#ifdef _WIN32
            int err = WSAGetLastError();
            if (err == WSAETIMEDOUT) continue;
            if (!open_.load()) break;
#else
            if (errno == EAGAIN || errno == EWOULDBLOCK) continue;
            if (!open_.load()) break;
#endif
            spdlog::warn("UDP recv error");
            continue;
        }

        mozart_input_frame_t frame{};
        if (!unpack_input_packet(buf.data(), static_cast<size_t>(n), frame)) {
            packets_dropped_.fetch_add(1, std::memory_order_relaxed);
            continue;
        }

        // 仅首个通过协议校验的发送方可成为回复目标。
        if (!client_known_.load()) {
            std::lock_guard<std::mutex> client_lock(client_mutex_);
            if (!client_known_.load()) {
                std::memcpy(&client_addr_, &src, static_cast<size_t>(src_len));
                client_addr_len_ = src_len;
                client_known_.store(true);
            }
        }

        packets_received_.fetch_add(1, std::memory_order_relaxed);

        {
            std::lock_guard<std::mutex> input_lock(input_mutex_);
            if (!open_.load() || !input_ring_) break;
            if (!input_ring_->push(&frame)) {
                mozart_input_frame_t discarded{};
                if (!input_ring_->pop(&discarded) || !input_ring_->push(&frame)) {
                    packets_dropped_.fetch_add(1, std::memory_order_relaxed);
                    continue;
                }
                packets_dropped_.fetch_add(1, std::memory_order_relaxed);
            }
        }
        input_cv_.notify_one();
    }
}

bool UdpStream::ReadFrame(void* out_frame_buf, uint32_t buf_size) {
    if (!out_frame_buf || direction_ != StreamDirection::Capture) return false;
    if (buf_size != sizeof(mozart_input_frame_t)) {
        spdlog::warn("UdpStream ReadFrame: buf_size {} != {}", buf_size, sizeof(mozart_input_frame_t));
        return false;
    }

    std::unique_lock<std::mutex> input_lock(input_mutex_);
    input_cv_.wait(input_lock, [this] {
        return (input_ring_ && input_ring_->readable_count() > 0)
            || !open_.load();
    });
    if (!open_.load() || !input_ring_) return false;

    const uint32_t pending = input_ring_->readable_count();
    if (pending > 4) {
        mozart_input_frame_t discarded{};
        for (uint32_t i = 1; i < pending; ++i) {
            if (!input_ring_->pop(&discarded)) break;
            packets_dropped_.fetch_add(1, std::memory_order_relaxed);
        }
    }
    return input_ring_->pop(out_frame_buf);
}

bool UdpStream::WriteFrame(const void* in_frame_buf, uint32_t buf_size) {
    if (!in_frame_buf || !open_.load()) return false;
    // Capture 方向也允许 WriteFrame（服务器回声：收 input → 推理 → 发 output 回客户端）
    if (buf_size != sizeof(mozart_output_frame_t)) {
        spdlog::warn("UdpStream WriteFrame: buf_size {} != {}", buf_size, sizeof(mozart_output_frame_t));
        return false;
    }
    if (!client_known_.load()) {
        spdlog::debug("UdpStream WriteFrame: no client address yet, dropping");
        return false;
    }

    OutputPacket pkt{};
    pack_output_packet(*static_cast<const mozart_output_frame_t*>(in_frame_buf), pkt);
    return send_to_client(pkt.data(), pkt.size());
}

bool UdpStream::send_to_client(const void* data, size_t len) {
    sockaddr_storage client_addr{};
    socket_len_t client_addr_len = 0;
    {
        std::lock_guard<std::mutex> client_lock(client_mutex_);
        if (!client_known_.load()) return false;
        client_addr = client_addr_;
        client_addr_len = client_addr_len_;
    }

    std::lock_guard<std::mutex> socket_lock(socket_mutex_);
    if (!open_.load() || !socket_valid(sock_fd_)) return false;
    const auto sent = ::sendto(
        sock_fd_, reinterpret_cast<const char*>(data), static_cast<int>(len), 0,
        reinterpret_cast<sockaddr*>(&client_addr), client_addr_len);
    if (sent < 0) {
        spdlog::warn("UDP send error");
        return false;
    }
    packets_sent_.fetch_add(1, std::memory_order_relaxed);
    return true;
}

std::string UdpStream::client_host() const {
    std::lock_guard<std::mutex> lk(client_mutex_);
    if (client_addr_.ss_family == AF_INET) {
        char buf[INET_ADDRSTRLEN] = {0};
        auto* a = reinterpret_cast<const sockaddr_in*>(&client_addr_);
        ::inet_ntop(AF_INET, &a->sin_addr, buf, sizeof(buf));
        return std::string(buf);
    }
    return {};
}

uint16_t UdpStream::client_port() const {
    std::lock_guard<std::mutex> lk(client_mutex_);
    if (client_addr_.ss_family == AF_INET) {
        auto* a = reinterpret_cast<const sockaddr_in*>(&client_addr_);
        return ntohs(a->sin_port);
    }
    return 0;
}

} // namespace mozart
