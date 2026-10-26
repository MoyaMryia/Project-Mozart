#include "runtime.hpp"
#include "wav.hpp"
#include "session.hpp"
#include <libusb.h>
#include <alsa/asoundlib.h>
#include <csignal>
#include <cstring>
#include <iostream>
#include <memory>

using namespace mozart_host;
namespace {
std::atomic<bool> interrupted{false};
static_assert(std::atomic<bool>::is_always_lock_free, "signal flag requires lock-free atomics");
void signal_handler(int) { interrupted.store(true, std::memory_order_relaxed); }
constexpr auto OperationTimeout = std::chrono::milliseconds(3 * MOZART_USB_IO_TIMEOUT_MS);
constexpr unsigned PollMs = 20;

struct Shared {
    std::mutex mutex;
    Status status;
    Failure failure;
    std::atomic<bool> playback_ready{false}, tx_done{false}, rx_done{false}, playback_done{false};
    std::atomic<uint64_t> stop_deadline_us{0};
    template<class F> void update(F f) { std::lock_guard<std::mutex> lock(mutex); f(status); }
    std::string snapshot() { std::lock_guard<std::mutex> lock(mutex); return status.json(); }
    void record(const std::exception& e) {
        std::lock_guard<std::mutex> lock(mutex);
        status.fail(e.what()); failure.record();
    }
};
class Usb {
    libusb_context* context_ = nullptr;
    libusb_device_handle* handle_ = nullptr;
    uint8_t in_ = 0, out_ = 0;
    bool claimed_ = false;
    void release() {
        if (handle_) { if (claimed_) libusb_release_interface(handle_, 1); libusb_close(handle_); handle_ = nullptr; }
        if (context_) { libusb_exit(context_); context_ = nullptr; }
    }
public:
    Usb() {
        try {
            require(libusb_init(&context_) == 0, "libusb_init failed");
            handle_ = libusb_open_device_with_vid_pid(context_, 0x1d6b, 0x0105);
            require(handle_, "USB 1d6b:0105 unavailable");
            libusb_config_descriptor* descriptor = nullptr;
            int rc = libusb_get_active_config_descriptor(libusb_get_device(handle_), &descriptor);
            require(rc == 0, std::string("USB active descriptor: ") + libusb_error_name(rc));
            std::unique_ptr<libusb_config_descriptor, decltype(&libusb_free_config_descriptor)> config(descriptor, libusb_free_config_descriptor);
            int selected_alt = -1;
            for (unsigned i = 0; i < config->bNumInterfaces; ++i) {
                for (int a = 0; a < config->interface[i].num_altsetting; ++a) {
                    const auto& alt = config->interface[i].altsetting[a];
                    if (alt.bInterfaceNumber != 1 || alt.bAlternateSetting != 0) continue;
                    uint8_t input = 0, output = 0; unsigned inputs = 0, outputs = 0;
                    for (unsigned e = 0; e < alt.bNumEndpoints; ++e) {
                        const auto& ep = alt.endpoint[e];
                        if ((ep.bmAttributes & LIBUSB_TRANSFER_TYPE_MASK) != LIBUSB_TRANSFER_TYPE_BULK) continue;
                        if (ep.bEndpointAddress & LIBUSB_ENDPOINT_IN) { input = ep.bEndpointAddress; ++inputs; }
                        else { output = ep.bEndpointAddress; ++outputs; }
                    }
                    if (inputs == 1 && outputs == 1 && (selected_alt < 0 || alt.bAlternateSetting == 0)) {
                        selected_alt = alt.bAlternateSetting; in_ = input; out_ = output;
                    }
                }
            }
            require(selected_alt >= 0, "interface 1 requires one bulk endpoint per direction");
            rc = libusb_claim_interface(handle_, 1);
            require(rc == 0, std::string("USB claim interface 1: ") + libusb_error_name(rc)); claimed_ = true;
            // interface 1 使用现有 alt 0，不重新启用 FunctionFS 的共享端点。
        } catch (const std::exception&) { release(); throw; }
    }
    ~Usb() { release(); }
    void send(const Frame& f) {
        auto b = encode(f); int transferred = 0;
        int rc = libusb_bulk_transfer(handle_, out_, b.data(), int(b.size()), &transferred, MOZART_USB_IO_TIMEOUT_MS);
        require(rc == 0, std::string("USB send: ") + libusb_error_name(rc));
        require(transferred == int(b.size()), "short USB send");
    }
    struct Read { std::vector<uint8_t> bytes; bool timeout; };
    Read read(unsigned timeout_ms) {
        Read r; r.bytes.resize(65536); int transferred = 0;
        int rc = libusb_bulk_transfer(handle_, in_, r.bytes.data(), int(r.bytes.size()), &transferred, timeout_ms);
        require(rc == 0 || rc == LIBUSB_ERROR_TIMEOUT, std::string("USB receive: ") + libusb_error_name(rc));
        r.bytes.resize(size_t(transferred)); r.timeout = rc == LIBUSB_ERROR_TIMEOUT; return r;
    }
};
void negotiate(const Options& o, Usb& usb, Receiver<Usb>& receiver, Shared& shared) {
    const auto selection = negotiate_session(o.legacy, usb, receiver);
    shared.update([&](Status& s) {
        s.block = selection.block; s.stream = selection.negotiated ? MOZART_USB_STREAM_FAST64 : MOZART_USB_STREAM_FAST;
        s.negotiated = selection.negotiated; s.fallback_reason = selection.fallback_reason;
    });
}
// Pcm 的启动、target 等待和非阻塞 drain 移植自实测 bridge.cpp。
class Pcm {
    Shared& shared_;
    bool capture_;
    std::string name_;
    snd_pcm_sframes_t target_available_ = 0;
    void check(int rc, const char* operation) {
        if (rc >= 0) return;
        if (rc == -EPIPE || rc == -ESTRPIPE) {
            shared_.update([&](Status& s) {
                if (rc == -EPIPE) { if (capture_) ++s.capture_overrun; else ++s.playback_underrun; }
                else { if (capture_) ++s.capture_suspend; else ++s.playback_suspend; }
            });
        }
        throw std::runtime_error(std::string(operation) + " (" + name_ + "): " + snd_strerror(rc));
    }
public:
    snd_pcm_t* pcm = nullptr;
    unsigned block;
    snd_pcm_uframes_t buffer;
    Pcm(const std::string& name, bool capture, unsigned frames, const Options& o, Shared& shared)
        : shared_(shared), capture_(capture), name_(name), block(frames), buffer(frames * o.periods) {
        try {
            check(snd_pcm_open(&pcm, name.c_str(), capture ? SND_PCM_STREAM_CAPTURE : SND_PCM_STREAM_PLAYBACK, SND_PCM_NONBLOCK), "PCM open");
            snd_pcm_hw_params_t* hw; snd_pcm_hw_params_alloca(&hw);
            check(snd_pcm_hw_params_any(pcm, hw), "hw_params_any");
            check(snd_pcm_hw_params_set_access(pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED), "PCM access");
            check(snd_pcm_hw_params_set_format(pcm, hw, SND_PCM_FORMAT_S16_LE), "PCM format");
            check(snd_pcm_hw_params_set_channels(pcm, hw, MOZART_USB_CHANNELS), "PCM channels");
            check(snd_pcm_hw_params_set_rate(pcm, hw, MOZART_USB_RATE, 0), "PCM rate");
            snd_pcm_uframes_t period = frames;
            check(snd_pcm_hw_params_set_period_size_near(pcm, hw, &period, nullptr), "PCM period");
            check(snd_pcm_hw_params_set_buffer_size_near(pcm, hw, &buffer), "PCM buffer");
            check(snd_pcm_hw_params(pcm, hw), "PCM hardware params");
            check(snd_pcm_hw_params_get_period_size(hw, &period, nullptr), "read PCM period");
            check(snd_pcm_hw_params_get_buffer_size(hw, &buffer), "read PCM buffer");
            require(period == frames && buffer == frames * o.periods, "PCM negotiated different period/buffer");
            snd_pcm_sw_params_t* sw; snd_pcm_sw_params_alloca(&sw);
            check(snd_pcm_sw_params_current(pcm, sw), "current PCM sw params");
            check(snd_pcm_sw_params_set_avail_min(pcm, sw, frames), "PCM avail_min");
            check(snd_pcm_sw_params_set_start_threshold(pcm, sw, capture ? 1 : frames * o.prefill), "PCM start threshold");
            check(snd_pcm_sw_params_set_stop_threshold(pcm, sw, buffer), "PCM stop threshold");
            check(snd_pcm_sw_params(pcm, sw), "PCM software params");
            check(snd_pcm_prepare(pcm), "PCM prepare");
        } catch (const std::exception&) { if (pcm) snd_pcm_close(pcm); throw; }
    }
    ~Pcm() { if (pcm) snd_pcm_close(pcm); }
    void start() { check(snd_pcm_start(pcm), "capture start"); }
    void stop_capture() { check(snd_pcm_drop(pcm), "capture stop"); }
    void set_target(unsigned target) {
        snd_pcm_sw_params_t* sw; snd_pcm_sw_params_alloca(&sw);
        check(snd_pcm_sw_params_current(pcm, sw), "target sw params");
        target_available_ = buffer - target * block + block;
        check(snd_pcm_sw_params_set_avail_min(pcm, sw, target_available_), "target avail_min");
        check(snd_pcm_sw_params(pcm, sw), "apply playback target");
    }
    int64_t delay() {
        snd_pcm_sframes_t value = 0; check(snd_pcm_delay(pcm, &value), "PCM delay");
        require(value >= 0, "negative PCM delay: " + name_);
        return value;
    }
    void wait_target() {
        auto deadline = Clock::now() + OperationTimeout;
        for (;;) {
            shared_.failure.check();
            auto n = snd_pcm_avail_update(pcm); if (n < 0) check(int(n), "playback availability");
            if (n >= target_available_) return;
            require(Clock::now() < deadline, "playback target timeout");
            int rc = snd_pcm_wait(pcm, PollMs); if (rc != -EINTR) check(rc, "playback target wait");
        }
    }
    void transfer(uint8_t* data, unsigned frames) {
        unsigned offset = 0; auto deadline = Clock::now() + OperationTimeout;
        while (offset < frames) {
            shared_.failure.check();
            require(Clock::now() < deadline, "PCM transfer timeout: " + name_);
            auto n = capture_ ? snd_pcm_readi(pcm, data + offset * FrameBytes, frames - offset)
                              : snd_pcm_writei(pcm, data + offset * FrameBytes, frames - offset);
            if (n == -EAGAIN || n == -EINTR) {
                int rc = snd_pcm_wait(pcm, PollMs); if (rc != -EINTR) check(rc, "PCM wait"); continue;
            }
            if (n < 0) check(int(n), capture_ ? "PCM capture" : "PCM playback");
            require(n > 0, "zero PCM transfer"); offset += unsigned(n);
        }
    }
    void drain() {
        shared_.failure.check();
        int rc = snd_pcm_drain(pcm);
        if (rc == 0) return;
        if (rc != -EAGAIN) check(rc, "PCM drain");
        auto deadline = Clock::now() + OperationTimeout;
        while (snd_pcm_state(pcm) != SND_PCM_STATE_SETUP) {
            shared_.failure.check();
            require(snd_pcm_state(pcm) == SND_PCM_STATE_DRAINING, "unexpected PCM drain state");
            require(Clock::now() < deadline, "PCM drain timeout");
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    }
};
struct PlaybackItem {
    uint32_t seq; unsigned valid; std::vector<uint8_t> bytes;
    uint64_t tx_sent_us;
    std::optional<int64_t> capture_delay_frames;
};
struct TraceItem {
    bool sent; uint32_t seq; unsigned valid;
    uint64_t host_us, stamp, rtt;
    std::vector<uint8_t> bytes;
};
constexpr size_t TraceLimit = 8192;
class Trace {
    const Options& options_;
    Shared& shared_;
    Queue<TraceItem, TraceLimit> queue_;
public:
    std::thread thread;
    Trace(const Options& o, Shared& s) : options_(o), shared_(s) {}
    void start() {
        if (options_.trace.empty()) return;
        std::filesystem::create_directories(options_.trace);
        thread = std::thread([this] {
            try {
                WavWriter input(options_.trace + "/capture.wav"), output(options_.trace + "/response.wav");
                RegularFile csv(options_.trace + "/packets.csv", true);
                std::string buffer = "direction,seq,host_us,wire_timestamp_us,rtt_us,source_frames\n";
                consume_trace(queue_, shared_.failure, [&](const TraceItem& t) {
                    (t.sent ? input : output).write(t.bytes.data(), t.valid);
                    buffer += std::string(t.sent ? "tx," : "rx,") + std::to_string(t.seq) + ',' + std::to_string(t.host_us) + ',' +
                        std::to_string(t.stamp) + ',' + std::to_string(t.rtt) + ',' + std::to_string(t.valid) + '\n';
                    if (buffer.size() >= 65536) { csv.write(buffer); buffer.clear(); }
                }, [&] {
                    finish_trace([&](const std::exception& e) {
                        shared_.update([&](Status& s) { if (s.trace_cleanup_reason.empty()) s.trace_cleanup_reason = e.what(); });
                        shared_.record(e);
                    }, [&] { csv.write(buffer); }, [&] { input.finish(); }, [&] { output.finish(); }, [&] { csv.close(); });
                });
            } catch (const std::exception& e) { shared_.record(e); }
        });
    }
    void put(bool sent, const Frame& f, unsigned valid, uint64_t host_us, uint64_t rtt = 0) {
        if (!options_.trace.empty()) queue_.put({sent, f.seq, valid, host_us, f.stamp, rtt, f.body});
    }
    void close() { queue_.close(); }
};
void paced_wait(Clock::time_point target, Shared& s) {
    while (Clock::now() < target && !interrupted) {
        s.failure.check(); std::this_thread::sleep_until(std::min(target, Clock::now() + std::chrono::milliseconds(PollMs)));
    }
}
void run(const Options& o, Shared& shared) {
    std::unique_ptr<WavReader> input;
    if (!o.input.empty()) {
        input = std::make_unique<WavReader>(o.input);
        shared.update([&](Status& s) { s.source_total_frames = input->frames; });
    }
    Usb usb; Receiver receiver(usb); negotiate(o, usb, receiver, shared);
    unsigned block; uint8_t stream; bool negotiated;
    {
        std::lock_guard<std::mutex> lock(shared.mutex);
        block = shared.status.block; stream = uint8_t(shared.status.stream); negotiated = shared.status.negotiated;
    }
    save_status(o.status, shared.snapshot());
    std::unique_ptr<Pcm> capture, playback;
    if (!input) {
        capture = std::make_unique<Pcm>(o.capture, true, block, o, shared);
        playback = std::make_unique<Pcm>(o.playback, false, block, o, shared);
    }
    Queue<PlaybackItem> queue; CaptureLedger<Ledger> ledger; Trace trace(o, shared);
    std::thread tx, rx, player;
    ThreadJoiner joiner{shared.failure, {&tx, &rx, &player, &trace.thread}};
    try {
    trace.start();
    player = std::thread([&] {
        try {
            std::unique_ptr<WavWriter> file;
            if (input) file = std::make_unique<WavWriter>(o.output);
            std::vector<uint8_t> zeros(size_t(block) * FrameBytes);
            if (playback) {
                for (unsigned i = 0; i < o.prefill; ++i) {
                    playback->transfer(zeros.data(), block);
                    shared.update([&](Status& s) { s.startup_silence_frames += block; });
                }
                auto delay = playback->delay();
                shared.update([&](Status& s) { s.playback_delay = delay; });
                require(snd_pcm_state(playback->pcm) == SND_PCM_STATE_RUNNING, "startup silence did not start playback");
                playback->set_target(o.target);
            }
            shared.playback_ready.store(true);
            bool live = false; uint64_t played = 0, written = 0;
            while (!queue.done()) {
                shared.failure.check();
                if (playback) playback->wait_target();
                PlaybackItem item;
                if (!queue.take(item, shared.failure, live ? 1 : 0)) {
                    if (queue.done()) break;
                    if (playback) {
                        if (!live) {
                            playback->transfer(zeros.data(), block);
                            shared.update([&](Status& s) { s.startup_silence_frames += block; });
                        } else {
                            auto delay = playback->delay();
                            shared.update([&](Status& s) { s.playback_delay = delay; });
                        }
                    } else std::this_thread::sleep_for(std::chrono::milliseconds(1));
                    continue;
                }
                require(item.seq == (file ? written : Warmup + played), "playback sequence mismatch");
                if (playback) {
                    playback->transfer(item.bytes.data(), block);
                    auto delay = playback->delay();
                    auto playback_now = now_us();
                    shared.update([&](Status& s) {
                        s.playback_delay = delay;
                        s.record_alsa_queue_estimate(item.seq, item.tx_sent_us, *item.capture_delay_frames, playback_now, delay);
                    });
                } else file->write(item.bytes.data(), item.valid);
                ++written;
                if (!file || item.seq >= Warmup) ++played;
                live = true;
                shared.update([&](Status& s) { s.played = played; s.output_frames += item.valid; });
            }
            shared.failure.check();
            if (playback) playback->drain();
            if (file) file->finish();
            shared.playback_done.store(true);
        } catch (const std::exception& e) { shared.record(e); }
    });
    rx = std::thread([&] {
        try {
            uint64_t received = 0;
            for (;;) {
                shared.failure.check();
                Frame f;
                if (!receiver.next(f, PollMs)) {
                    ledger.check_deadline();
                    stop_deadline(now_us(), shared.stop_deadline_us.load()); continue;
                }
                uint64_t arrived = now_us();
                stop_deadline(arrived, shared.stop_deadline_us.load());
                if (audio(f)) {
                    require(f.stream == stream && !f.flags && f.body.size() == size_t(block) * FrameBytes, "invalid audio response");
                    auto p = ledger.receive(f, negotiated, arrived); ++received;
                    trace.put(false, f, p.valid, arrived, arrived - p.sent);
                    shared.update([&](Status& s) {
                        s.rx = received; s.rtt.add(arrived - p.sent);
                        if (received <= Warmup) ++s.warmup;
                        if (received == Warmup) s.ready();
                    });
                    if (input || received > Warmup) queue.put({f.seq, p.valid, std::move(f.body), p.sent, p.capture_delay_frames});
                } else if (control(f)) {
                    uint64_t sent;
                    { std::lock_guard<std::mutex> lock(shared.mutex); sent = shared.status.tx; }
                    require(shared.stop_deadline_us.load() != 0, "unsolicited control response");
                    stop_ack(f, sent, received);
                    receiver.finish(); queue.close(); shared.rx_done.store(true); break;
                }
                ledger.check_deadline();
            }
        } catch (const std::exception& e) { shared.record(e); queue.close(); }
    });
    tx = std::thread([&] {
        try {
            while (!shared.playback_ready.load()) {
                shared.failure.check(); std::this_thread::sleep_for(std::chrono::milliseconds(1));
            }
            if (capture && !interrupted) capture->start();
            uint64_t source_frames = 0, count = 0;
            uint64_t limit = o.seconds ? o.seconds * MOZART_USB_RATE : UINT64_MAX;
            if (input) limit = std::min(limit, input->frames);
            auto target = Clock::now(); uint64_t fraction = 0;
            while (!interrupted && source_frames < limit) {
                shared.failure.check();
                if (input && o.pace) { paced_wait(target, shared); if (interrupted) break; }
                require(count < UINT32_MAX, "USB sequence/count range exhausted");
                unsigned valid = unsigned(std::min<uint64_t>(block, limit - source_frames));
                Frame f; f.stream = stream; f.seq = uint32_t(count); f.body.resize(size_t(block) * FrameBytes);
                std::optional<int64_t> capture_delay_frames;
                if (input) valid = input->read(f.body, valid);
                else { capture->transfer(f.body.data(), valid); capture_delay_frames = capture->delay(); }
                if (interrupted) break;
                f.stamp = now_us(); ledger.submit({f.seq, f.stamp, valid, capture_delay_frames});
                // 在发送前公布计数，防止 RX 比 send 返回更早收到回复。
                ++count; source_frames += valid;
                shared.update([&](Status& s) {
                    s.tx = count; s.source_frames = source_frames; s.padding_frames += block - valid;
                    if (capture_delay_frames) s.capture_delay = *capture_delay_frames;
                });
                trace.put(true, f, valid, f.stamp); usb.send(f);
                fraction += uint64_t(block) * 1000000000;
                target += std::chrono::nanoseconds(fraction / MOZART_USB_RATE); fraction %= MOZART_USB_RATE;
            }
            shared.failure.check();
            // 先让 Jetson 排空末窗，再停止采集；声卡 drop 的等待不能阻塞末包。
            shared.stop_deadline_us.store(now_us() + uint64_t(std::chrono::duration_cast<std::chrono::microseconds>(OperationTimeout).count()));
            usb.send(command(MOZART_USB_CONTROL_STOP, 2));
            if (capture) capture->stop_capture();
            shared.tx_done.store(true);
        } catch (const std::exception& e) { shared.record(e); queue.close(); }
    });
    while (!shared.failure.failed.load() &&
           !(shared.tx_done.load() && shared.rx_done.load() && shared.playback_done.load())) {
        auto sizes = queue.sizes(); shared.update([&](Status& s) { s.queue_current = sizes.first; s.queue_peak = sizes.second; });
        save_status(o.status, shared.snapshot());
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    tx.join(); rx.join(); player.join(); trace.close();
    if (trace.thread.joinable()) trace.thread.join();
    auto sizes = queue.sizes(); shared.update([&](Status& s) { s.queue_current = sizes.first; s.queue_peak = sizes.second; });
    shared.failure.check();
    shared.update([&](Status& s) {
        require(s.played == s.rx - s.warmup && s.warmup == std::min<uint64_t>(Warmup, s.rx), "final playback/warmup coverage mismatch");
        if (input) require(s.output_frames == s.source_frames, "file output source coverage mismatch");
        s.stop();
    });
    } catch (const std::exception& e) { shared.record(e); throw; }
}
}
int main(int argc, char** argv) {
    Shared shared; Options options;
    try { options = parse(std::vector<std::string>(argv + 1, argv + argc)); }
    catch (const std::exception& e) { std::cerr << "invalid arguments: " << e.what() << '\n'; return 2; }
    try {
        std::signal(SIGINT, signal_handler); std::signal(SIGTERM, signal_handler);
        save_status(options.status, shared.snapshot());
        run(options, shared); save_status(options.status, shared.snapshot()); return 0;
    } catch (const std::exception& e) {
        shared.update([&](Status& s) { s.fail(e.what()); });
        std::cerr << "failed: " << e.what() << '\n';
        try { save_status(options.status, shared.snapshot()); }
        catch (const std::exception& status_error) { std::cerr << "status persistence failed: " << status_error.what() << '\n'; }
        return 1;
    }
}
