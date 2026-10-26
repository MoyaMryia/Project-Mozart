#pragma once
#include "mozart/usb_protocol_generated.h"
#include "file.hpp"
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <climits>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <fstream>
#include <limits>
#include <mutex>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace mozart_host {
using Clock = std::chrono::steady_clock;
constexpr unsigned Warmup = MOZART_USB_NATIVE_WARMUP_PACKETS;
constexpr uint64_t MaxDigitalLatencyUs = MOZART_USB_DIGITAL_LATENCY_BUDGET_US;
constexpr size_t QueueLimit = 8;
constexpr unsigned FrameBytes = MOZART_USB_CHANNELS * MOZART_USB_SAMPLE_BYTES;
inline void require(bool ok, const std::string& reason) {
    if (!ok) throw std::runtime_error(reason);
}
inline uint64_t now_us() {
    return uint64_t(std::chrono::duration_cast<std::chrono::microseconds>(Clock::now().time_since_epoch()).count());
}
inline uint16_t u16(const uint8_t* p) { return uint16_t(p[0]) | uint16_t(p[1]) << 8; }
inline uint32_t u32(const uint8_t* p) {
    return uint32_t(p[0]) | uint32_t(p[1]) << 8 | uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
}
inline uint64_t u64(const uint8_t* p) { return u32(p) | uint64_t(u32(p + 4)) << 32; }
inline void put16(uint8_t* p, uint16_t v) { p[0] = uint8_t(v); p[1] = uint8_t(v >> 8); }
inline void put32(uint8_t* p, uint32_t v) { for (unsigned i = 0; i < 4; ++i) p[i] = uint8_t(v >> (8 * i)); }
inline void put64(uint8_t* p, uint64_t v) { put32(p, uint32_t(v)); put32(p + 4, uint32_t(v >> 32)); }
struct Frame {
    uint8_t type = MOZART_USB_TYPE_PCM, stream = MOZART_USB_STREAM_FAST, flags = 0;
    uint32_t seq = 0;
    uint64_t stamp = 0;
    std::vector<uint8_t> body;
};
inline std::vector<uint8_t> encode(const Frame& f) {
    std::vector<uint8_t> b(MOZART_USB_HEADER_BYTES + f.body.size());
    put16(b.data() + MOZART_USB_MAGIC_OFFSET, MOZART_USB_MAGIC);
    b[MOZART_USB_VERSION_OFFSET] = MOZART_USB_VERSION;
    b[MOZART_USB_TYPE_OFFSET] = f.type; b[MOZART_USB_STREAM_OFFSET] = f.stream;
    b[MOZART_USB_FLAGS_OFFSET] = f.flags;
    put32(b.data() + MOZART_USB_SEQ_OFFSET, f.seq);
    put64(b.data() + MOZART_USB_TIMESTAMP_OFFSET, f.stamp);
    put32(b.data() + MOZART_USB_LENGTH_OFFSET, uint32_t(f.body.size()));
    std::copy(f.body.begin(), f.body.end(), b.begin() + MOZART_USB_HEADER_BYTES);
    return b;
}
// 每个头只在接收边界校验一次。缓冲区最多保存一个有界帧。
class Decoder {
    std::vector<uint8_t> bytes_;
    size_t required_ = MOZART_USB_HEADER_BYTES;
    bool header_ = false;
public:
    bool partial() const { return !bytes_.empty(); }
    void finish() const { require(!partial(), "truncated USB frame"); }
    std::vector<Frame> push(const uint8_t* p, size_t n) {
        std::vector<Frame> result;
        while (n) {
            size_t take = std::min(n, required_ - bytes_.size());
            bytes_.insert(bytes_.end(), p, p + take); p += take; n -= take;
            if (bytes_.size() != required_) continue;
            if (!header_) {
                const auto* h = bytes_.data();
                require(u16(h + MOZART_USB_MAGIC_OFFSET) == MOZART_USB_MAGIC, "invalid USB magic");
                require(h[MOZART_USB_VERSION_OFFSET] == MOZART_USB_VERSION, "unsupported USB version");
                require(u16(h + MOZART_USB_RESERVED_OFFSET) == 0, "nonzero USB reserved field");
                constexpr unsigned KnownFlags = MOZART_USB_FLAG_START | MOZART_USB_FLAG_END | MOZART_USB_FLAG_URGENT;
                require((h[MOZART_USB_FLAGS_OFFSET] & ~KnownFlags) == 0, "unknown USB flags");
                uint32_t length = u32(h + MOZART_USB_LENGTH_OFFSET);
                require(length <= MOZART_USB_MAX_PAYLOAD, "USB payload exceeds schema limit");
                required_ += length; header_ = true;
                if (bytes_.size() != required_) continue;
            }
            const auto* h = bytes_.data();
            Frame f; f.type = h[MOZART_USB_TYPE_OFFSET]; f.stream = h[MOZART_USB_STREAM_OFFSET];
            f.flags = h[MOZART_USB_FLAGS_OFFSET]; f.seq = u32(h + MOZART_USB_SEQ_OFFSET);
            f.stamp = u64(h + MOZART_USB_TIMESTAMP_OFFSET);
            f.body.assign(bytes_.begin() + MOZART_USB_HEADER_BYTES, bytes_.end());
            result.push_back(std::move(f)); bytes_.clear(); required_ = MOZART_USB_HEADER_BYTES; header_ = false;
        }
        return result;
    }
};
inline bool control(const Frame& f) {
    return f.type == MOZART_USB_TYPE_CONTROL && f.stream == MOZART_USB_STREAM_CONTROL;
}
inline bool audio(const Frame& f) {
    return f.type == MOZART_USB_TYPE_PCM &&
        (f.stream == MOZART_USB_STREAM_FAST || f.stream == MOZART_USB_STREAM_FAST64);
}
inline Frame command(uint16_t op, size_t length) {
    Frame f; f.type = MOZART_USB_TYPE_CONTROL; f.stream = MOZART_USB_STREAM_CONTROL;
    f.stamp = now_us(); f.body.resize(length); put16(f.body.data(), op); return f;
}
inline Frame hello() {
    auto f = command(MOZART_USB_CONTROL_HELLO, MOZART_USB_HELLO_BYTES);
    f.body[2] = MOZART_USB_VERSION;
    put32(f.body.data() + 3, MOZART_USB_CAP_CONTROL | MOZART_USB_CAP_FAST64);
    put16(f.body.data() + 7, MOZART_USB_FAST64_SAMPLES); return f;
}
inline unsigned hello_ack(const Frame& f) {
    require(!f.flags && f.body.size() == MOZART_USB_HELLO_BYTES, "invalid HELLO_ACK shape");
    require(u16(f.body.data()) == MOZART_USB_CONTROL_HELLO_ACK && f.body[2] == MOZART_USB_VERSION,
            "invalid HELLO_ACK opcode/version");
    unsigned block = u16(f.body.data() + 7);
    require(block == MOZART_USB_FAST_SAMPLES || block == MOZART_USB_FAST64_SAMPLES, "unsupported HELLO_ACK block");
    require(block != MOZART_USB_FAST64_SAMPLES || (u32(f.body.data() + 3) & MOZART_USB_CAP_FAST64),
            "HELLO_ACK selected64 without capability");
    return block;
}
inline void start_ack(const Frame& f, unsigned block) {
    require(!f.flags && f.body.size() == MOZART_USB_START_BYTES, "invalid START_ACK shape");
    require(u16(f.body.data()) == MOZART_USB_CONTROL_START_ACK && u16(f.body.data() + 2) == block,
            "START_ACK mismatch");
}
struct SessionSelection {
    unsigned block = MOZART_USB_FAST_SAMPLES;
    bool negotiated = false;
    std::string fallback_reason;
};
template<class Transport, class Receive>
SessionSelection negotiate_session(bool legacy, Transport& usb, Receive& receiver) {
    SessionSelection selection; Frame ack;
    if (legacy) selection.fallback_reason = "legacy_requested";
    else {
        usb.send(hello());
        if (!receiver.handshake(ack, MOZART_USB_HELLO_TIMEOUT_MS, true,
                               [&](const Frame& f) { selection.block = hello_ack(f); })) {
            selection.block = MOZART_USB_FAST_SAMPLES;
            selection.fallback_reason = "hello_timeout_legacy128";
        } else if (selection.block == MOZART_USB_FAST_SAMPLES) selection.fallback_reason = "hello_selected_legacy128";
    }
    auto start = command(MOZART_USB_CONTROL_START, MOZART_USB_START_BYTES);
    put16(start.body.data() + 2, uint16_t(selection.block)); usb.send(start);
    const bool legacy_block = selection.block == MOZART_USB_FAST_SAMPLES;
    if (!receiver.handshake(ack, MOZART_USB_IO_TIMEOUT_MS, legacy_block,
                            [&](const Frame& f) { start_ack(f, selection.block); }))
        selection.fallback_reason += ";start_ack_timeout_legacy128";
    selection.negotiated = !legacy_block;
    return selection;
}
inline void stop_ack(const Frame& f, uint64_t tx, uint64_t rx) {
    require(!f.flags && f.body.size() == MOZART_USB_STOP_ACK_BYTES, "invalid STOP_ACK shape");
    require(u16(f.body.data()) == MOZART_USB_CONTROL_STOP_ACK, "invalid STOP_ACK opcode");
    require(u32(f.body.data() + 2) == tx && u32(f.body.data() + 6) == rx && tx == rx,
            "STOP_ACK coverage mismatch");
}
inline uint64_t integer(const std::string& s) {
    require(!s.empty(), "empty integer"); uint64_t v = 0;
    for (char c : s) {
        require(c >= '0' && c <= '9', "expected unsigned decimal integer: " + s);
        unsigned d = unsigned(c - '0');
        require(v <= (UINT64_MAX - d) / 10, "integer overflow"); v = v * 10 + d;
    }
    return v;
}
struct ArtifactCollision : std::runtime_error { using std::runtime_error::runtime_error; };
struct Options {
    std::string capture = "default", playback = "default", input, output, trace;
    std::string status = "build/run/usb-native.json";
    uint64_t seconds = 0;
    unsigned periods = 6, prefill = 4, target = 3;
    bool legacy = false, pace = false;
};
inline Options parse(const std::vector<std::string>& args) {
    Options o; bool devices = false;
    for (size_t i = 0; i < args.size(); ++i) {
        const std::string& k = args[i];
        auto value = [&]() { require(i + 1 < args.size(), "missing value for " + k); return args[++i]; };
        auto text = [&]() { auto s = value(); require(!s.empty(), "empty value for " + k); return s; };
        auto count = [&]() { auto n = integer(value()); require(n <= UINT_MAX, "option exceeds unsigned range"); return unsigned(n); };
        if (k == "--capture") { o.capture = text(); devices = true; }
        else if (k == "--playback") { o.playback = text(); devices = true; }
        else if (k == "--input") o.input = text();
        else if (k == "--output") o.output = text();
        else if (k == "--trace-dir") o.trace = text();
        else if (k == "--status-file") o.status = text();
        else if (k == "--seconds") o.seconds = integer(value());
        else if (k == "--periods") o.periods = count();
        else if (k == "--prefill") o.prefill = count();
        else if (k == "--target") o.target = count();
        else if (k == "--legacy") o.legacy = true;
        else if (k == "--pace") o.pace = true;
        else throw std::runtime_error("unknown option: " + k);
    }
    require(o.periods >= 3 && o.periods <= UINT_MAX / MOZART_USB_FAST_SAMPLES &&
            o.prefill >= 2 && o.prefill < o.periods && o.target >= 2 && o.target < o.periods,
            "invalid periods/prefill/target");
    require(o.seconds <= UINT64_MAX / MOZART_USB_RATE, "seconds sample count overflow");
    require(o.input.empty() == o.output.empty(), "--input and --output must be specified together");
    require(o.input.empty() || !devices, "file mode excludes ALSA device options");
    require(!o.pace || !o.input.empty(), "--pace requires file mode");
    std::vector<std::string> paths{o.status, o.status + ".tmp"};
    if (!o.input.empty()) { paths.push_back(o.input); paths.push_back(o.output); }
    if (!o.trace.empty()) {
        paths.push_back(o.trace + "/capture.wav"); paths.push_back(o.trace + "/response.wav");
        paths.push_back(o.trace + "/packets.csv");
    }
    for (size_t i = 0; i < paths.size(); ++i) for (size_t j = 0; j < i; ++j) {
        if (std::filesystem::weakly_canonical(paths[i]) == std::filesystem::weakly_canonical(paths[j]))
            throw ArtifactCollision("input, output, status and trace paths must differ");
        if (std::filesystem::exists(paths[i]) && std::filesystem::exists(paths[j]) && std::filesystem::equivalent(paths[i], paths[j]))
            throw ArtifactCollision("artifact paths refer to the same file");
    }
    return o;
}
struct Failure {
    std::atomic<bool> failed{false};
    std::mutex mutex;
    std::exception_ptr error;
    void record(std::exception_ptr e = std::current_exception()) {
        std::lock_guard<std::mutex> lock(mutex);
        if (!error) error = e;
        failed.store(true);
    }
    void check() {
        if (failed.load()) { std::lock_guard<std::mutex> lock(mutex); std::rethrow_exception(error); }
    }
};
// 来自实测 bridge.cpp：先取消，再按生命周期回收线程。
struct ThreadJoiner {
    Failure& failure;
    std::vector<std::thread*> threads;
    ~ThreadJoiner() {
        for (auto* t : threads) if (t->joinable()) {
            failure.record(std::make_exception_ptr(std::runtime_error("worker setup interrupted"))); break;
        }
        for (auto* t : threads) if (t->joinable()) t->join();
    }
};
template<class T, size_t Capacity = QueueLimit> class Queue {
    std::mutex mutex_;
    std::condition_variable changed_;
    std::deque<T> items_;
    size_t peak_ = 0;
    bool closed_ = false;
public:
    void put(T item) {
        std::lock_guard<std::mutex> lock(mutex_);
        require(!closed_, "queue closed"); require(items_.size() < Capacity, "queue full");
        items_.push_back(std::move(item)); peak_ = std::max(peak_, items_.size()); changed_.notify_one();
    }
    bool take(T& item, Failure& failure, unsigned wait_ms = 10) {
        std::unique_lock<std::mutex> lock(mutex_);
        changed_.wait_for(lock, std::chrono::milliseconds(wait_ms), [&] { return closed_ || !items_.empty() || failure.failed.load(); });
        failure.check();
        if (items_.empty()) return false;
        item = std::move(items_.front()); items_.pop_front(); return true;
    }
    bool done() { std::lock_guard<std::mutex> lock(mutex_); return closed_ && items_.empty(); }
    std::pair<size_t, size_t> sizes() { std::lock_guard<std::mutex> lock(mutex_); return {items_.size(), peak_}; }
    void close() { std::lock_guard<std::mutex> lock(mutex_); closed_ = true; changed_.notify_all(); }
};
// 保留原有协议关联校验，并把采集队列深度与同一待回复包关联。
template<class Association> class CaptureLedger {
    Association association_;
    std::mutex mutex_;
    std::deque<std::optional<int64_t>> capture_delays_;
public:
    struct Pending {
        uint32_t seq;
        uint64_t sent;
        unsigned valid;
        std::optional<int64_t> capture_delay_frames;
    };
    void submit(Pending p) {
        std::lock_guard<std::mutex> lock(mutex_);
        association_.submit({p.seq, p.sent, p.valid});
        capture_delays_.push_back(p.capture_delay_frames);
    }
    Pending receive(const Frame& f, bool negotiated, uint64_t arrived) {
        std::lock_guard<std::mutex> lock(mutex_);
        auto p = association_.receive(f, negotiated, arrived);
        Pending result{p.seq, p.sent, p.valid, capture_delays_.front()};
        capture_delays_.pop_front(); return result;
    }
    void check_deadline() { association_.check_deadline(); }
};
// 全局失败时只保存已消费记录，再把原异常交给线程入口。
template<class T, size_t Capacity, class Consume, class Finish>
void consume_trace(Queue<T, Capacity>& queue, Failure& failure, Consume consume, Finish finish) {
    while (!queue.done()) {
        T item;
        try {
            if (!queue.take(item, failure)) continue;
            consume(item);
        } catch (const std::exception&) { finish(); throw; }
    }
    finish();
}
// 每项收尾独立执行；报告回调保存错误，原始队列异常继续上抛。
template<class Report, class... Actions> void finish_trace(Report report, Actions... actions) {
    auto save = [&](auto& action) {
        try { action(); } catch (const std::exception& e) { report(e); }
    };
    (save(actions), ...);
}
// 以 32 us 桶宽记录 0..131040 us；末桶保存更大的值，max 保留精确值。
struct Histogram {
    static constexpr uint64_t Width = 32;
    std::array<uint64_t, 4096> bins{};
    uint64_t count = 0, maximum = 0;
    void add(uint64_t v) { ++bins[std::min<size_t>(v / Width, bins.size() - 1)]; ++count; maximum = std::max(v, maximum); }
    uint64_t percentile(unsigned percent) const {
        if (!count) return 0;
        uint64_t target = count / 100 * percent + ((count % 100) * percent + 99) / 100, sum = 0;
        for (size_t i = 0; i < bins.size(); ++i) {
            sum += bins[i];
            if (sum >= target) return i == bins.size() - 1 ? maximum : std::min(maximum, (i + 1) * Width - 1);
        }
        return maximum;
    }
};
inline std::string json_string(const std::string& s) {
    static const char hex[] = "0123456789abcdef";
    std::string out = "\"";
    for (size_t i = 0; i < s.size(); ++i) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        if (c == '"' || c == '\\') { out += '\\'; out += char(c); }
        else if (c < 0x20) { out += "\\u00"; out += hex[c >> 4]; out += hex[c & 15]; }
        else if (c < 0x80) out += char(c);
        else {
            size_t n = c >= 0xc2 && c <= 0xdf ? 2 : c >= 0xe0 && c <= 0xef ? 3 : c >= 0xf0 && c <= 0xf4 ? 4 : 0;
            bool valid = n && i + n <= s.size();
            for (size_t j = 1; valid && j < n; ++j) valid = (static_cast<unsigned char>(s[i + j]) & 0xc0) == 0x80;
            if (valid && n >= 3) {
                unsigned char b = static_cast<unsigned char>(s[i + 1]);
                valid = !(c == 0xe0 && b < 0xa0) && !(c == 0xed && b >= 0xa0) &&
                        !(c == 0xf0 && b < 0x90) && !(c == 0xf4 && b >= 0x90);
            }
            if (valid) { out.append(s, i, n); i += n - 1; }
            else { out += "\\u00"; out += hex[c >> 4]; out += hex[c & 15]; }
        }
    }
    return out + '"';
}
struct Status {
    std::string state = "warming", reason, fallback_reason, trace_cleanup_reason;
    unsigned block = 0, stream = 0;
    bool negotiated = false;
    uint64_t tx = 0, rx = 0, played = 0, warmup = 0, source_frames = 0, source_total_frames = 0;
    uint64_t padding_frames = 0, startup_silence_frames = 0;
    uint64_t capture_overrun = 0, playback_underrun = 0, capture_suspend = 0, playback_suspend = 0;
    uint64_t output_frames = 0;
    size_t queue_current = 0, queue_peak = 0;
    int64_t capture_delay = 0, playback_delay = 0;
    Histogram rtt, alsa_e2e_queue_estimate_us;
    uint64_t alsa_e2e_over_budget = 0, alsa_e2e_max_delta_us = 0;
    uint32_t alsa_e2e_max_seq = 0;
    int64_t alsa_e2e_max_capture_frames = 0, alsa_e2e_max_playback_frames = 0;
    void record_alsa_queue_estimate(uint32_t seq, uint64_t sent, int64_t capture_frames,
                                    uint64_t playback_now, int64_t playback_frames) {
        // 最老采样的采集块长项与播放队列头的块长项抵消。
        // now - sent 已包含软件等待，不另加 WOLA 常数。
        uint64_t delta = playback_now - sent;
        uint64_t estimate = delta + uint64_t(capture_frames + playback_frames) * 1000000 / MOZART_USB_RATE;
        if (!alsa_e2e_queue_estimate_us.count || estimate > alsa_e2e_queue_estimate_us.maximum) {
            alsa_e2e_max_seq = seq; alsa_e2e_max_capture_frames = capture_frames;
            alsa_e2e_max_playback_frames = playback_frames; alsa_e2e_max_delta_us = delta;
        }
        alsa_e2e_queue_estimate_us.add(estimate);
        if (estimate > MaxDigitalLatencyUs) ++alsa_e2e_over_budget;
    }
    void ready() { if (state == "warming") state = "ready"; }
    void stop() { if (state != "failed") { state = "stopped"; reason = "normal_stop"; } }
    void fail(const std::string& why) { if (state != "failed") { state = "failed"; reason = why; } }
    std::string json() const {
        std::ostringstream o;
        o << "{\"state\":" << json_string(state) << ",\"reason\":" << json_string(reason)
          << ",\"fallback_reason\":" << json_string(fallback_reason)
          << ",\"trace_cleanup_reason\":" << json_string(trace_cleanup_reason)
          << ",\"negotiated\":" << (negotiated ? "true" : "false") << ",\"block\":" << block << ",\"stream\":" << stream
          << ",\"tx\":" << tx << ",\"rx\":" << rx << ",\"played\":" << played << ",\"warmup\":" << warmup
          << ",\"source_frames\":" << source_frames << ",\"source_total_frames\":" << source_total_frames
          << ",\"padding_frames\":" << padding_frames << ",\"output_frames\":" << output_frames
          << ",\"startup_silence_frames\":" << startup_silence_frames
          << ",\"queue_current\":" << queue_current << ",\"queue_peak\":" << queue_peak
          << ",\"xrun\":{\"capture_overrun\":" << capture_overrun << ",\"playback_underrun\":" << playback_underrun
          << ",\"capture_suspend\":" << capture_suspend << ",\"playback_suspend\":" << playback_suspend << "}"
          << ",\"rtt_us\":{\"count\":" << rtt.count << ",\"p50\":" << rtt.percentile(50)
          << ",\"p99\":" << rtt.percentile(99) << ",\"max\":" << rtt.maximum << ",\"bucket_us\":" << Histogram::Width << "}"
          << ",\"alsa_e2e_queue_estimate_us\":{\"estimated\":true,\"physical_measurement\":false,\"warmup_excluded_first_packets\":" << Warmup
          << ",\"count\":" << alsa_e2e_queue_estimate_us.count << ",\"p50\":" << alsa_e2e_queue_estimate_us.percentile(50)
          << ",\"p99\":" << alsa_e2e_queue_estimate_us.percentile(99) << ",\"max\":" << alsa_e2e_queue_estimate_us.maximum
          << ",\"bucket_us\":" << Histogram::Width << ",\"budget_us\":" << MaxDigitalLatencyUs
          << ",\"over_budget_count\":" << alsa_e2e_over_budget
          << ",\"max_evidence\":{\"seq\":" << alsa_e2e_max_seq << ",\"capture_frames\":" << alsa_e2e_max_capture_frames
          << ",\"playback_frames\":" << alsa_e2e_max_playback_frames << ",\"delta_us\":" << alsa_e2e_max_delta_us << "}}"
          << ",\"alsa_queue_estimate\":{\"estimated\":true,\"capture_frames\":" << capture_delay
          << ",\"playback_frames\":" << playback_delay << "}}\n";
        return o.str();
    }
};
inline void save_status(const std::string& path, const std::string& json) {
    auto dest = std::filesystem::path(path);
    if (dest.has_parent_path()) std::filesystem::create_directories(dest.parent_path());
    auto tmp = dest; tmp += ".tmp";
    RegularFile out(tmp.string(), true); out.write(json); out.close();
    std::filesystem::rename(tmp, dest);
}
}
