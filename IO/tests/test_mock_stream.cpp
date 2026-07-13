// test_mock_stream.cpp — IO 流驱动测试
// ============================================================================
// 覆盖：
//   1. PipeWireStream stub：Capture 产静音帧 + frame_idx 递增；Playback 计数
//   2. UdpStream 回声：Capture 收 input_frame → WriteFrame 发 output_frame 回发送方
//   3. MockStream：生成临时 WAV，Capture 读帧验证 PCM + meta
//   4. C-ABI mozart_io_create_pipewire_stream + read/write_frame
#include "mozart/pipewire_stream.hpp"
#ifdef MOZART_IO_ENABLE_UDP
#include "mozart/udp_stream.hpp"
#endif
#include "mozart/mock_stream.hpp"
#include "mozart/audio_io.h"
#include "mozart/frame_meta.h"

#include <spdlog/spdlog.h>
#include <cassert>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <thread>
#include <atomic>
#include <array>
#include <vector>
#include <fstream>
#include <random>
#include <chrono>
#include <future>
#include <limits>

#ifdef MOZART_IO_ENABLE_UDP
#ifdef _WIN32
  #include <winsock2.h>
  #include <ws2tcpip.h>
  #pragma comment(lib, "ws2_32.lib")
#else
  #include <sys/socket.h>
  #include <netinet/in.h>
  #include <arpa/inet.h>
  #include <unistd.h>
#endif
#endif

static int g_failures = 0;
#define CHECK(cond) do { \
    if (!(cond)) { std::printf("FAIL [%s:%d] %s\n", __FILE__, __LINE__, #cond); ++g_failures; } \
} while (0)

// ---- PipeWireStream stub -----------------------------------------------------
static void test_pipewire_stub() {
    using namespace mozart;
    PipeWireStream cap("default_mic", StreamDirection::Capture);
    StreamConfig cfg; cfg.direction = StreamDirection::Capture;
    cfg.sample_rate = 48000; cfg.frame_duration_ms = 20;
    CHECK(cap.Open(cfg));

    mozart_raw_frame_t f{};
    for (int i = 0; i < 5; ++i) {
        CHECK(cap.ReadFrame(&f, sizeof(f)));
        CHECK(f.meta.frame_idx == static_cast<uint32_t>(i));
        CHECK(f.pcm[0] == 0.0f);  // stub 产静音
    }
    CHECK(cap.frames_processed() == 5);
    cap.Close();

    PipeWireStream pb("virtual_sink", StreamDirection::Playback);
    cfg.direction = StreamDirection::Playback;
    CHECK(pb.Open(cfg));
    mozart_output_frame_t of{};
    for (int i = 0; i < 3; ++i) CHECK(pb.WriteFrame(&of, sizeof(of)));
    CHECK(pb.frames_processed() == 3);
    pb.Close();
    std::printf("[OK] test_pipewire_stub\n");
}

// ---- C-ABI mozart_io_create_pipewire_stream ---------------------------------
static void test_cabi_pipewire() {
    mozart_stream_handle_t h = mozart_io_create_pipewire_stream("default", MOZART_IO_DIR_CAPTURE);
    CHECK(h != nullptr);
    CHECK(mozart_io_open_stream(h, MOZART_RAW_SAMPLE_RATE,
                                MOZART_RAW_FRAME_MS, 16));
    CHECK(mozart_io_is_stream_open(h));
    mozart_raw_frame_t f{};
    CHECK(mozart_io_read_frame(h, &f, sizeof(f)));
    mozart_io_close_stream(h);
    CHECK(!mozart_io_is_stream_open(h));
    mozart_io_destroy_stream(h);
    std::printf("[OK] test_cabi_pipewire\n");
}

#ifdef MOZART_IO_ENABLE_UDP
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
    packet[17] = 200;
    packet[18] = 255;
    packet[19] = 1;
    for (uint32_t i = 0; i < MOZART_INPUT_SAMPLES; ++i) {
        const float value = static_cast<float>(i) / MOZART_INPUT_SAMPLES;
        std::memcpy(packet.data() + MOZART_PACKET_HEADER_SIZE
                    + i * sizeof(float), &value, sizeof(value));
    }
    return packet;
}

template <typename GetCount>
bool wait_for_count(GetCount&& get_count, uint64_t expected) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
    while (get_count() < expected && std::chrono::steady_clock::now() < deadline) {
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    return get_count() >= expected;
}

void close_socket(socket_t socket) {
#ifdef _WIN32
    closesocket(socket);
#else
    ::close(socket);
#endif
}

} // namespace

// ---- UdpStream 回声：Capture 收 input → 发 output 回发送方 -------------------
static void test_udp_echo() {
    using namespace mozart;
    const uint16_t port = 19001;
    UdpStream cap("127.0.0.1", port, StreamDirection::Capture);
    StreamConfig cfg; cfg.direction = StreamDirection::Capture;
    cfg.sample_rate = MOZART_INPUT_SAMPLE_RATE;
    cfg.frame_duration_ms = MOZART_INPUT_FRAME_MS;
    CHECK(cap.Open(cfg));

    const socket_t malformed_sender = ::socket(AF_INET, SOCK_DGRAM, 0);
    sockaddr_in dst{};
    dst.sin_family = AF_INET;
    dst.sin_port = htons(port);
    ::inet_pton(AF_INET, "127.0.0.1", &dst.sin_addr);
    const std::array<unsigned char, 4> invalid_packet{{0, 1, 2, 3}};
    ::sendto(malformed_sender,
             reinterpret_cast<const char*>(invalid_packet.data()),
             static_cast<int>(invalid_packet.size()), 0,
             reinterpret_cast<sockaddr*>(&dst), sizeof(dst));
    CHECK(wait_for_count([&cap] { return cap.packets_dropped(); }, 1));
    CHECK(!cap.client_known());

    std::atomic<bool> done{false};
    std::atomic<bool> echo_ok{false};

    // 辅助线程：发送 input_frame 包 → 等待收到 output_frame 回包
    std::thread sender([&] {
#ifdef _WIN32
        WSADATA wsa; WSAStartup(MAKEWORD(2,2), &wsa);
#endif
        socket_t s = ::socket(AF_INET, SOCK_DGRAM, 0);
        timeval timeout{2, 0};
        ::setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        const auto pkt = make_input_packet(7);
        ::sendto(s, reinterpret_cast<const char*>(pkt.data()),
                 static_cast<int>(pkt.size()), 0,
                 reinterpret_cast<sockaddr*>(&dst), sizeof(dst));

        // 等待回包
        std::vector<unsigned char> rbuf(8192);
        sockaddr_in from{}; socket_len_t fl = sizeof(from);
        auto n = ::recvfrom(s, reinterpret_cast<char*>(rbuf.data()),
                            static_cast<int>(rbuf.size()), 0,
                            reinterpret_cast<sockaddr*>(&from), &fl);
        if (n == static_cast<decltype(n)>(
                MOZART_PACKET_HEADER_SIZE + MOZART_OUTPUT_SAMPLES * 4)) {
            uint32_t mg = 0; std::memcpy(&mg, rbuf.data(), 4);
            if (mg == MOZART_PACKET_MAGIC) echo_ok = true;
        }
        close_socket(s);
        done = true;
    });

    // 主线程：Capture 收 input → 验证 → WriteFrame 发 output 回客户端
    mozart_input_frame_t in{};
    CHECK(cap.ReadFrame(&in, sizeof(in)));
    CHECK(in.meta.frame_idx == 7);
    CHECK(in.pcm[0] == 0.0f);
    CHECK(in.pcm[1] == 1.0f / MOZART_INPUT_SAMPLES);

    mozart_output_frame_t out{};
    out.meta = in.meta;
    for (int i = 0; i < MOZART_OUTPUT_SAMPLES; ++i) out.pcm[i] = 0.5f;
    CHECK(cap.WriteFrame(&out, sizeof(out)));  // 回声：发回已知客户端

    // 等待 sender 完成
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    sender.join();
    CHECK(done.load());
    CHECK(echo_ok.load());
    CHECK(cap.packets_received() >= 1);
    CHECK(cap.packets_sent() >= 1);
    cap.Close();
    close_socket(malformed_sender);
    std::printf("[OK] test_udp_echo\n");
}

static void test_udp_backlog_recovery() {
    using namespace mozart;
    constexpr uint16_t port = 19002;
    UdpStream cap("127.0.0.1", port, StreamDirection::Capture);
    StreamConfig cfg;
    cfg.direction = StreamDirection::Capture;
    cfg.sample_rate = MOZART_INPUT_SAMPLE_RATE;
    cfg.frame_duration_ms = MOZART_INPUT_FRAME_MS;
    cfg.ring_capacity = 8;
    CHECK(cap.Open(cfg));

    const socket_t sender = ::socket(AF_INET, SOCK_DGRAM, 0);
    sockaddr_in dst{};
    dst.sin_family = AF_INET;
    dst.sin_port = htons(port);
    ::inet_pton(AF_INET, "127.0.0.1", &dst.sin_addr);
    for (uint32_t i = 0; i < 12; ++i) {
        const auto packet = make_input_packet(i);
        ::sendto(sender, reinterpret_cast<const char*>(packet.data()),
                 static_cast<int>(packet.size()), 0,
                 reinterpret_cast<sockaddr*>(&dst), sizeof(dst));
    }
    CHECK(wait_for_count([&cap] { return cap.packets_received(); }, 12));

    mozart_input_frame_t latest{};
    CHECK(cap.ReadFrame(&latest, sizeof(latest)));
    CHECK(latest.meta.frame_idx == 11);
    CHECK(cap.packets_dropped() >= 11);

    close_socket(sender);
    cap.Close();
    std::printf("[OK] test_udp_backlog_recovery\n");
}

static void test_udp_close_unblocks_reader() {
    using namespace mozart;
    constexpr uint16_t port = 19003;
    UdpStream cap("127.0.0.1", port, StreamDirection::Capture);
    StreamConfig cfg;
    cfg.direction = StreamDirection::Capture;
    cfg.sample_rate = MOZART_INPUT_SAMPLE_RATE;
    CHECK(cap.Open(cfg));

    auto read_result = std::async(std::launch::async, [&cap] {
        mozart_input_frame_t frame{};
        return cap.ReadFrame(&frame, sizeof(frame));
    });
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
    cap.Close();
    CHECK(read_result.wait_for(std::chrono::seconds(1)) == std::future_status::ready);
    CHECK(!read_result.get());

    CHECK(!cap.Open(StreamConfig{StreamDirection::Capture,
                                 MOZART_INPUT_SAMPLE_RATE,
                                 MOZART_INPUT_FRAME_MS,
                                 std::numeric_limits<uint32_t>::max()}));
    CHECK(!cap.IsOpen());
    CHECK(cap.Open(cfg));
    cap.Close();

    UdpStream invalid("not-an-ip", port, StreamDirection::Capture);
    CHECK(!invalid.Open(cfg));
    std::printf("[OK] test_udp_close_unblocks_reader\n");
}
#endif // MOZART_IO_ENABLE_UDP

// ---- MockStream：生成临时 WAV，Capture 读帧 ---------------------------------
namespace {

void write_temp_wav(const std::string& path, uint32_t sample_rate, uint32_t samples) {
    std::ofstream f(path, std::ios::binary);
    const uint32_t data_bytes = samples * sizeof(float);
    const uint32_t riff_size = 36 + data_bytes;
    f.write("RIFF", 4); f.write(reinterpret_cast<const char*>(&riff_size), 4);
    f.write("WAVE", 4);
    f.write("fmt ", 4);
    const uint32_t fmt_size = 16; f.write(reinterpret_cast<const char*>(&fmt_size), 4);
    const uint16_t audio_format = 3; f.write(reinterpret_cast<const char*>(&audio_format), 2);
    const uint16_t channels = 1;    f.write(reinterpret_cast<const char*>(&channels), 2);
    f.write(reinterpret_cast<const char*>(&sample_rate), 4);
    const uint32_t byte_rate = sample_rate * 4; f.write(reinterpret_cast<const char*>(&byte_rate), 4);
    const uint16_t block_align = 4; f.write(reinterpret_cast<const char*>(&block_align), 2);
    const uint16_t bits = 32;       f.write(reinterpret_cast<const char*>(&bits), 2);
    f.write("data", 4); f.write(reinterpret_cast<const char*>(&data_bytes), 4);
    std::vector<float> pcm(samples);
    for (uint32_t i = 0; i < samples; ++i)
        pcm[i] = 0.5f * std::sin(2.0f * 3.14159265f * 440.0f * i / sample_rate);
    f.write(reinterpret_cast<const char*>(pcm.data()), data_bytes);
}

void write_invalid_wav_bits(const std::string& path) {
    std::ofstream f(path, std::ios::binary);
    const uint32_t riff_size = 37;
    const uint32_t fmt_size = 16;
    const uint16_t audio_format = 1;
    const uint16_t channels = 1;
    const uint32_t sample_rate = 48000;
    const uint32_t byte_rate = 0;
    const uint16_t block_align = 0;
    const uint16_t bits = 4;
    const uint32_t data_bytes = 1;
    const unsigned char sample = 0;
    f.write("RIFF", 4); f.write(reinterpret_cast<const char*>(&riff_size), 4);
    f.write("WAVE", 4); f.write("fmt ", 4);
    f.write(reinterpret_cast<const char*>(&fmt_size), 4);
    f.write(reinterpret_cast<const char*>(&audio_format), 2);
    f.write(reinterpret_cast<const char*>(&channels), 2);
    f.write(reinterpret_cast<const char*>(&sample_rate), 4);
    f.write(reinterpret_cast<const char*>(&byte_rate), 4);
    f.write(reinterpret_cast<const char*>(&block_align), 2);
    f.write(reinterpret_cast<const char*>(&bits), 2);
    f.write("data", 4); f.write(reinterpret_cast<const char*>(&data_bytes), 4);
    f.write(reinterpret_cast<const char*>(&sample), 1);
}

} // namespace

static void test_mock_stream() {
    using namespace mozart;
    const std::string tmp_wav = "test_mock_stream_tmp.wav";
    const uint32_t sr = 48000;
    const uint32_t total_samples = sr;  // 1 秒
    write_temp_wav(tmp_wav, sr, total_samples);

    MockStream cap(tmp_wav, StreamDirection::Capture, sr, 20);
    StreamConfig cfg; cfg.direction = StreamDirection::Capture;
    cfg.sample_rate = sr; cfg.frame_duration_ms = 20;
    CHECK(cap.Open(cfg));

    mozart_raw_frame_t f{};
    CHECK(cap.ReadFrame(&f, sizeof(f)));
    CHECK(f.meta.frame_idx == 0);
    CHECK(f.meta.vad_flag == 1);
    // 首样本应近似 0（正弦波起点）
    CHECK(std::fabs(f.pcm[0]) < 0.01f);

    for (int i = 1; i < 10; ++i) {
        CHECK(cap.ReadFrame(&f, sizeof(f)));
        CHECK(f.meta.frame_idx == static_cast<uint32_t>(i));
    }
    CHECK(cap.frames_read() == 10);
    cap.Close();
    std::remove(tmp_wav.c_str());

    const std::string mismatched_wav = "test_mock_stream_rate_tmp.wav";
    write_temp_wav(mismatched_wav, 44100, 44100);
    MockStream mismatched(mismatched_wav, StreamDirection::Capture, sr, 20);
    CHECK(!mismatched.Open(cfg));
    std::remove(mismatched_wav.c_str());

    const std::string malformed_wav = "test_mock_stream_invalid_tmp.wav";
    write_invalid_wav_bits(malformed_wav);
    MockStream malformed(malformed_wav, StreamDirection::Capture, sr, 20);
    CHECK(!malformed.Open(cfg));
    std::remove(malformed_wav.c_str());
    std::printf("[OK] test_mock_stream\n");
}

int main() {
    spdlog::set_level(spdlog::level::warn);  // 抑制 info 日志
    test_pipewire_stub();
    test_cabi_pipewire();
#ifdef MOZART_IO_ENABLE_UDP
    test_udp_echo();
    test_udp_backlog_recovery();
    test_udp_close_unblocks_reader();
#endif
    test_mock_stream();

    if (g_failures == 0) {
        std::printf("\nAll IO stream tests passed.\n");
        return 0;
    }
    std::printf("\n%d failures.\n", g_failures);
    return 1;
}
