// test_mock_stream.cpp — IO 流驱动测试
// ============================================================================
// 覆盖：
//   1. PipeWireStream stub：Capture 产静音帧 + frame_idx 递增；Playback 计数
//   2. UdpStream 回声：Capture 收 input_frame → WriteFrame 发 output_frame 回发送方
//   3. MockStream：生成临时 WAV，Capture 读帧验证 PCM + meta
//   4. C-ABI mozart_io_create_pipewire_stream + read/write_frame
#include "mozart/pipewire_stream.hpp"
#include "mozart/udp_stream.hpp"
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
#include <vector>
#include <fstream>
#include <random>
#include <chrono>

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

// ---- UdpStream 回声：Capture 收 input → 发 output 回发送方 -------------------
static void test_udp_echo() {
    using namespace mozart;
    const uint16_t port = 19001;
    UdpStream cap("127.0.0.1", port, StreamDirection::Capture);
    StreamConfig cfg; cfg.direction = StreamDirection::Capture;
    cfg.sample_rate = MOZART_INPUT_SAMPLE_RATE;
    cfg.frame_duration_ms = MOZART_INPUT_FRAME_MS;
    CHECK(cap.Open(cfg));

    std::atomic<bool> done{false};
    std::atomic<bool> echo_ok{false};

    // 辅助线程：发送 input_frame 包 → 等待收到 output_frame 回包
    std::thread sender([&] {
#ifdef _WIN32
        WSADATA wsa; WSAStartup(MAKEWORD(2,2), &wsa);
#endif
        int s = ::socket(AF_INET, SOCK_DGRAM, 0);
        timeval timeout{2, 0};
        ::setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        sockaddr_in dst{}; dst.sin_family = AF_INET;
        dst.sin_port = htons(port);
        ::inet_pton(AF_INET, "127.0.0.1", &dst.sin_addr);

        // 构造 input_frame MZRT 包（1300B）
        std::vector<unsigned char> pkt(MOZART_PACKET_HEADER_SIZE + MOZART_INPUT_SAMPLES * 4);
        uint32_t magic = MOZART_PACKET_MAGIC;
        std::memcpy(pkt.data(), &magic, 4);
        uint64_t pts = 12345; std::memcpy(pkt.data() + 4, &pts, 8);
        uint32_t idx = 7;     std::memcpy(pkt.data() + 12, &idx, 4);
        pkt[16] = 1; pkt[17] = 200; pkt[18] = 255; pkt[19] = 1;
        for (int i = 0; i < MOZART_INPUT_SAMPLES; ++i) {
            float v = static_cast<float>(i) / MOZART_INPUT_SAMPLES;
            std::memcpy(pkt.data() + 20 + i * 4, &v, 4);
        }
        ::sendto(s, pkt.data(), pkt.size(), 0, reinterpret_cast<sockaddr*>(&dst), sizeof(dst));

        // 等待回包
        std::vector<unsigned char> rbuf(8192);
        sockaddr_in from{}; socklen_t fl = sizeof(from);
        ssize_t n = ::recvfrom(s, rbuf.data(), rbuf.size(), 0,
                               reinterpret_cast<sockaddr*>(&from), &fl);
        if (n == static_cast<ssize_t>(MOZART_PACKET_HEADER_SIZE + MOZART_OUTPUT_SAMPLES * 4)) {
            uint32_t mg = 0; std::memcpy(&mg, rbuf.data(), 4);
            if (mg == MOZART_PACKET_MAGIC) echo_ok = true;
        }
#ifdef _WIN32
        closesocket(s);
#else
        ::close(s);
#endif
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
    std::printf("[OK] test_udp_echo\n");
}

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
    std::printf("[OK] test_mock_stream\n");
}

int main() {
    spdlog::set_level(spdlog::level::warn);  // 抑制 info 日志
    test_pipewire_stub();
    test_cabi_pipewire();
    test_udp_echo();
    test_mock_stream();

    if (g_failures == 0) {
        std::printf("\nAll IO stream tests passed.\n");
        return 0;
    }
    std::printf("\n%d failures.\n", g_failures);
    return 1;
}
