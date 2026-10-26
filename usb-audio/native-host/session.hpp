#pragma once
#include "runtime.hpp"

namespace mozart_host {
constexpr size_t PendingLimit = 4096;
template<class Transport> class Receiver {
    Transport& usb_;
    Decoder decoder_;
    std::deque<Frame> frames_;
public:
    explicit Receiver(Transport& usb) : usb_(usb) {}
    bool partial() const { return decoder_.partial(); }
    void finish() const {
        decoder_.finish();
        for (const auto& f : frames_) require(!audio(f) && !control(f), "unexpected frames after STOP_ACK");
    }
    bool next(Frame& f, unsigned timeout_ms) {
        if (frames_.empty()) {
            auto r = usb_.read(timeout_ms);
            auto parsed = decoder_.push(r.bytes.data(), r.bytes.size());
            for (auto& frame : parsed) frames_.push_back(std::move(frame));
        }
        if (frames_.empty()) return false;
        f = std::move(frames_.front()); frames_.pop_front(); return true;
    }
    template<class Validate> bool handshake(Frame& f, unsigned timeout_ms, bool allow_timeout, Validate validate) {
        auto deadline = Clock::now() + std::chrono::milliseconds(timeout_ms);
        while (Clock::now() < deadline) {
            auto left = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - Clock::now()).count();
            Frame candidate;
            if (!next(candidate, unsigned(std::max<int64_t>(1, left)))) continue;
            if (!control(candidate)) {
                require(!audio(candidate), "audio before negotiation commit"); continue;
            }
            validate(candidate);
            if (Clock::now() >= deadline) break;
            f = std::move(candidate); return true;
        }
        require(!partial(), "truncated handshake response");
        require(allow_timeout, "START_ACK timeout"); return false;
    }
};
inline void stop_deadline(uint64_t arrived, uint64_t deadline) {
    require(!deadline || arrived < deadline, "STOP_ACK timeout");
}
struct Pending {
    uint32_t seq;
    uint64_t sent;
    unsigned valid;
};
class Ledger {
    std::mutex mutex_;
    std::deque<Pending> pending_;
public:
    void submit(Pending p) {
        std::lock_guard<std::mutex> lock(mutex_);
        require(pending_.size() < PendingLimit, "USB inflight metadata full"); pending_.push_back(p);
    }
    Pending receive(const Frame& f, bool negotiated, uint64_t arrived) {
        std::lock_guard<std::mutex> lock(mutex_);
        require(!pending_.empty() && pending_.front().seq == f.seq, "response sequence mismatch");
        auto p = pending_.front();
        require(!negotiated || f.stamp == p.sent, "response host timestamp mismatch");
        require(arrived >= p.sent, "host monotonic clock reversed");
        require(arrived - p.sent <= uint64_t(MOZART_USB_IO_TIMEOUT_MS) * 1000, "USB response deadline exceeded");
        pending_.pop_front(); return p;
    }
    void check_deadline() {
        std::lock_guard<std::mutex> lock(mutex_);
        require(pending_.empty() || now_us() - pending_.front().sent <= uint64_t(MOZART_USB_IO_TIMEOUT_MS) * 1000,
                "USB response deadline exceeded");
    }
};
}
