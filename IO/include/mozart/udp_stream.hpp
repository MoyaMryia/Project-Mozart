// udp_stream.hpp — 实时网络 UDP 契约包驱动
// ============================================================================
// 从 rvc-backend/network/udp_server 抽出的纯 IO 部分（收包/客户端追踪/发包），
// 移除推理回调与 VAD bypass（这些属于 rvc-backend 业务层，由调用方在
// ReadFrame → 推理 → WriteFrame 之间自行处理）。
//
// 数据格式（与 frame_meta.h 对齐）：
//   Capture  → ReadFrame  产出 mozart_input_frame_t  (16kHz / 1296B / MZRT 包 1300B)
//              WriteFrame 消费 mozart_output_frame_t (48kHz / 3856B / MZRT 包 3860B)
//                          发回已知客户端地址（服务器回声场景：收 input → 推理 → 发 output）
//   Playback → WriteFrame 消费 mozart_output_frame_t (48kHz / 3856B)
//              ReadFrame  不可用（纯发送端）
//
// 客户端追踪：Capture 流记录首个合法发送方地址，WriteFrame 自动回发该地址；
//             Playback 流在构造时传入目标地址。
#ifndef MOZART_UDP_STREAM_HPP
#define MOZART_UDP_STREAM_HPP

#include "mozart/audio_stream.hpp"
#include "mozart/ring_buffer.hpp"
#include <memory>
#include <string>
#include <thread>
#include <atomic>
#include <mutex>
#include <condition_variable>
#include <cstdint>

#ifdef _WIN32
  #include <winsock2.h>
  #include <ws2tcpip.h>
  using socket_t = SOCKET;
  using socket_len_t = int;
#else
  #include <sys/socket.h>
  #include <netinet/in.h>
  using socket_t = int;
  using socket_len_t = socklen_t;
#endif

namespace mozart {

class UdpStream : public RealTimeAudioStream {
public:
    // host: Capture=本地绑定地址, Playback=对端地址
    // port: UDP 端口
    UdpStream(std::string host, uint16_t port, StreamDirection direction);
    ~UdpStream() override;

    StreamDirection GetDirection() const noexcept override { return direction_; }
    bool IsOpen() const noexcept override { return open_.load(); }

    bool Open(const StreamConfig& config) override;
    void Close() override;

    bool ReadFrame (void* out_frame_buf, uint32_t buf_size) override;
    bool WriteFrame(const void* in_frame_buf, uint32_t buf_size) override;

    uint64_t GetUnderlyingLatencyNs() const noexcept override { return 0; }

    // Capture 流：首个合法发送方地址（供配对 Playback 流使用）
    bool client_known() const noexcept { return client_known_.load(); }
    std::string client_host() const;
    uint16_t    client_port() const;

    // 统计观测口
    uint64_t packets_received() const noexcept { return packets_received_.load(); }
    uint64_t packets_sent()     const noexcept { return packets_sent_.load(); }
    uint64_t packets_dropped()  const noexcept { return packets_dropped_.load(); }

private:
    std::string      host_;
    uint16_t         port_;
    StreamDirection  direction_;

#ifdef _WIN32
    socket_t         sock_fd_{INVALID_SOCKET};
#else
    socket_t         sock_fd_{-1};
#endif
    std::atomic<bool> open_{false};
    std::mutex        lifecycle_mutex_;
    std::mutex        socket_mutex_;
#ifdef _WIN32
    bool              wsa_started_{false};
#endif

    // Capture: 接收线程 + 输入缓冲
    std::thread      recv_thread_;
    std::mutex       input_mutex_;
    std::condition_variable input_cv_;
    std::unique_ptr<SpscRing> input_ring_;

    // 客户端地址追踪（Capture 记录，Playback 发送目标）
    mutable std::mutex client_mutex_;
    sockaddr_storage client_addr_{};
    socket_len_t     client_addr_len_{0};
    std::atomic<bool> client_known_{false};

    std::atomic<uint64_t> packets_received_{0};
    std::atomic<uint64_t> packets_sent_{0};
    std::atomic<uint64_t> packets_dropped_{0};

    void receive_loop();
    bool send_to_client(const void* data, size_t len);
};

} // namespace mozart

#endif // MOZART_UDP_STREAM_HPP
