#include "pipewire_pcm.hpp"

#include <pipewire/context.h>
#include <pipewire/core.h>
#include <pipewire/pipewire.h>
#include <pipewire/stream.h>
#include <pipewire/thread-loop.h>
#include <spa/param/audio/format-utils.h>
#include <spa/param/audio/raw.h>
#include <spa/pod/builder.h>
#include <spa/utils/dict.h>
#include <spa/utils/result.h>

#include <algorithm>
#include <condition_variable>
#include <cstring>
#include <cstdlib>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <vector>

namespace mozart_host {
namespace {

constexpr unsigned PollMs = 20;

// Byte ring shared by the PipeWire callback and the USB worker. The callback
// never allocates; overflow/underrun are counted and reported.
class Ring {
public:
    explicit Ring(size_t capacity) : data_(capacity), capacity_(capacity) {}

    size_t size() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return count_;
    }

    void push_capture(const uint8_t* p, size_t n, uint64_t& dropped) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (n > capacity_) {
            p += n - capacity_;
            dropped += n - capacity_;
            n = capacity_;
            head_ = 0;
            count_ = 0;
        }
        if (count_ + n > capacity_) {
            size_t drop = count_ + n - capacity_;
            head_ = (head_ + drop) % capacity_;
            count_ -= drop;
            dropped += drop;
        }
        copy_in(p, n);
        changed_.notify_all();
    }

    bool push_playback(const uint8_t* p, size_t n) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (capacity_ - count_ < n) return false;
        copy_in(p, n);
        changed_.notify_all();
        return true;
    }

    bool pop(uint8_t* out, size_t n, std::chrono::milliseconds timeout, Failure& failure) {
        std::unique_lock<std::mutex> lock(mutex_);
        changed_.wait_for(lock, timeout, [&] { return count_ >= n || failure.failed.load(); });
        failure.check();
        if (count_ < n) return false;
        copy_out(out, n);
        changed_.notify_all();
        return true;
    }

    bool wait_low(size_t limit, std::chrono::milliseconds timeout, Failure& failure) {
        std::unique_lock<std::mutex> lock(mutex_);
        changed_.wait_for(lock, timeout, [&] { return count_ <= limit || failure.failed.load(); });
        failure.check();
        return count_ <= limit;
    }

    bool wait_space(size_t n, std::chrono::milliseconds timeout, Failure& failure) {
        std::unique_lock<std::mutex> lock(mutex_);
        changed_.wait_for(lock, timeout, [&] { return capacity_ - count_ >= n || failure.failed.load(); });
        failure.check();
        return capacity_ - count_ >= n;
    }

    // Returns bytes copied from the ring; the rest of out is zero-filled.
    size_t pop_or_silence(uint8_t* out, size_t n) {
        std::lock_guard<std::mutex> lock(mutex_);
        size_t take = std::min(n, count_);
        if (take) copy_out(out, take);
        if (take < n) std::memset(out + take, 0, n - take);
        changed_.notify_all();
        return take;
    }

    bool empty(std::chrono::milliseconds timeout, Failure& failure) {
        std::unique_lock<std::mutex> lock(mutex_);
        changed_.wait_for(lock, timeout, [&] { return count_ == 0 || failure.failed.load(); });
        failure.check();
        return count_ == 0;
    }

private:
    void copy_in(const uint8_t* p, size_t n) {
        size_t tail = (head_ + count_) % capacity_;
        size_t first = std::min(n, capacity_ - tail);
        std::memcpy(data_.data() + tail, p, first);
        if (first < n) std::memcpy(data_.data(), p + first, n - first);
        count_ += n;
    }
    void copy_out(uint8_t* p, size_t n) {
        size_t first = std::min(n, capacity_ - head_);
        std::memcpy(p, data_.data() + head_, first);
        if (first < n) std::memcpy(p + first, data_.data(), n - first);
        head_ = (head_ + n) % capacity_;
        count_ -= n;
    }

    std::vector<uint8_t> data_;
    size_t capacity_;
    size_t head_ = 0;
    size_t count_ = 0;
    mutable std::mutex mutex_;
    std::condition_variable changed_;
};

} // namespace

struct Engine {
    pw_thread_loop* loop = nullptr;
    pw_context* context = nullptr;
    pw_core* core = nullptr;

    Engine() {
        static std::once_flag once;
        std::call_once(once, [] { pw_init(nullptr, nullptr); });
        loop = pw_thread_loop_new("mozart-usb-host", nullptr);
        if (!loop) throw std::runtime_error("PipeWire thread loop create failed");
        const char* remote = std::getenv("PIPEWIRE_REMOTE");
        pw_properties* context_props = nullptr;
        if (remote && *remote) context_props = pw_properties_new(PW_KEY_REMOTE_NAME, remote, nullptr);
        context = pw_context_new(pw_thread_loop_get_loop(loop), context_props, 0);
        if (!context) {
            pw_thread_loop_destroy(loop); loop = nullptr;
            throw std::runtime_error("PipeWire context create failed");
        }
        core = pw_context_connect(context, nullptr, 0);
        if (!core) {
            pw_context_destroy(context); context = nullptr;
            pw_thread_loop_destroy(loop); loop = nullptr;
            throw std::runtime_error("PipeWire core connect failed");
        }
        if (pw_thread_loop_start(loop) < 0) {
            pw_core_disconnect(core); core = nullptr;
            pw_context_destroy(context); context = nullptr;
            pw_thread_loop_destroy(loop); loop = nullptr;
            throw std::runtime_error("PipeWire thread loop start failed");
        }
    }
    ~Engine() {
        if (core) pw_core_disconnect(core);
        if (context) pw_context_destroy(context);
        if (loop) { pw_thread_loop_stop(loop); pw_thread_loop_destroy(loop); }
    }
};

Engine& engine() {
    static Engine instance;
    return instance;
}

struct PipeWirePcm::Impl {
    bool capture;
    unsigned block;
    unsigned periods;
    std::string target;
    Failure& failure;
    Xrun xrun;
    Ring ring;
    unsigned target_frames = 3;

    Engine* engine = nullptr;
    pw_stream* stream = nullptr;
    std::atomic<bool> running{false};
    std::atomic<bool> active{false};
    std::atomic<bool> failed{false};
    std::atomic<uint64_t> drops{0};
    std::atomic<uint64_t> underruns{0};

    pw_registry* registry = nullptr;
    spa_hook registry_hook{};
    spa_hook stream_hook{};
    std::mutex registry_mutex;
    std::condition_variable registry_cv;
    uint32_t target_id = PW_ID_ANY;
    bool target_found = false;

    Impl(bool capture_, unsigned block_, unsigned periods_, std::string target_,
         Failure& failure_, Xrun xrun_)
        : capture(capture_), block(block_), periods(periods_), target(std::move(target_)),
          failure(failure_), xrun(std::move(xrun_)),
          ring(size_t(periods_) * block_ * FrameBytes) {}

    void on_process() {
        pw_buffer* buffer = pw_stream_dequeue_buffer(stream);
        if (!buffer) return;
        spa_buffer* buf = buffer->buffer;
        if (!buf || buf->n_datas == 0 || !buf->datas[0].data) {
            pw_stream_queue_buffer(stream, buffer);
            return;
        }
        auto* data = static_cast<uint8_t*>(buf->datas[0].data);
        const size_t max_bytes = buf->datas[0].maxsize - buf->datas[0].maxsize % FrameBytes;
        spa_data& d = buf->datas[0];
        if (capture) {
            size_t bytes = d.chunk ? d.chunk->size : max_bytes;
            bytes -= bytes % FrameBytes;
            if (bytes) {
                uint64_t dropped = 0;
                ring.push_capture(data, bytes, dropped);
                if (dropped) {
                    drops.fetch_add(dropped, std::memory_order_relaxed);
                    xrun(true, true);
                }
            }
            if (d.chunk) { d.chunk->offset = 0; d.chunk->stride = FrameBytes; d.chunk->size = bytes; }
        } else {
            size_t bytes = max_bytes;
            size_t got = ring.pop_or_silence(data, bytes);
            if (got != bytes) {
                ++underruns;
                xrun(false, false);
            }
            if (d.chunk) { d.chunk->offset = 0; d.chunk->stride = FrameBytes; d.chunk->size = bytes; }
        }
        pw_stream_queue_buffer(stream, buffer);
    }

    static void process_cb(void* data) { static_cast<Impl*>(data)->on_process(); }

    static void global_cb(void* data, uint32_t id, uint32_t, const char* type, uint32_t,
                          const spa_dict* props) {
        auto* self = static_cast<Impl*>(data);
        if (!props || !type || std::strcmp(type, "PipeWire:Interface:Node") != 0) return;
        const char* name = spa_dict_lookup(props, PW_KEY_NODE_NAME);
        if (!name || self->target != name) return;
        std::lock_guard<std::mutex> lock(self->registry_mutex);
        self->target_id = id;
        self->target_found = true;
        self->registry_cv.notify_all();
    }

    static void state_cb(void* data, pw_stream_state, pw_stream_state state, const char* error) {
        auto* self = static_cast<Impl*>(data);
        if (state == PW_STREAM_STATE_ERROR) {
            self->failed.store(true);
            self->failure.record(std::make_exception_ptr(
                std::runtime_error(std::string("PipeWire stream error: ") + (error ? error : "unknown"))));
        } else if (state == PW_STREAM_STATE_STREAMING) {
            self->running.store(true);
        } else if (state == PW_STREAM_STATE_UNCONNECTED) {
            self->running.store(false);
        }
    }
};

PipeWirePcm::PipeWirePcm(bool capture, unsigned block, unsigned periods, std::string target,
                         Failure& failure, Xrun xrun)
    : impl_(new Impl(capture, block, periods, std::move(target), failure, std::move(xrun))) {
    Impl& i = *impl_;
    i.engine = &engine();
    pw_thread_loop_lock(i.engine->loop);
    i.registry = pw_core_get_registry(i.engine->core, PW_VERSION_REGISTRY, 0);
    static const pw_registry_events registry_events = {
        PW_VERSION_REGISTRY_EVENTS, Impl::global_cb, nullptr,
    };
    pw_registry_add_listener(i.registry, &i.registry_hook, &registry_events, &i);
    pw_thread_loop_unlock(i.engine->loop);

    const std::string node_name = capture ? "mozart-usb-capture" : "mozart-usb-playback";
    const std::string latency = std::to_string(block) + "/" + std::to_string(MOZART_USB_RATE);
    pw_properties* props = pw_properties_new(
        PW_KEY_MEDIA_TYPE, "Audio",
        PW_KEY_MEDIA_CATEGORY, capture ? "Capture" : "Playback",
        PW_KEY_MEDIA_ROLE, "DSP",
        PW_KEY_NODE_NAME, node_name.c_str(),
        PW_KEY_NODE_LATENCY, latency.c_str(),
        nullptr);
    if (!props) throw std::runtime_error("PipeWire properties create failed");
    if (capture) pw_properties_set(props, PW_KEY_STREAM_CAPTURE_SINK, "true");

    static const pw_stream_events events = {
        PW_VERSION_STREAM_EVENTS,
        nullptr,
        Impl::state_cb,
        nullptr,
        nullptr,
        nullptr,
        nullptr,
        nullptr,
        Impl::process_cb,
        nullptr,
        nullptr,
        nullptr,
    };
    pw_thread_loop_lock(i.engine->loop);
    i.stream = pw_stream_new(i.engine->core, "mozart-usb", props);
    if (i.stream) pw_stream_add_listener(i.stream, &i.stream_hook, &events, &i);
    pw_thread_loop_unlock(i.engine->loop);
    if (!i.stream) throw std::runtime_error("PipeWire stream create failed");

    uint8_t pod_buffer[1024];
    spa_pod_builder builder = SPA_POD_BUILDER_INIT(pod_buffer, sizeof(pod_buffer));
    spa_audio_info_raw info{};
    info.format = SPA_AUDIO_FORMAT_S16_LE;
    info.rate = MOZART_USB_RATE;
    info.channels = MOZART_USB_CHANNELS;
    info.position[0] = SPA_AUDIO_CHANNEL_FL;
    info.position[1] = SPA_AUDIO_CHANNEL_FR;
    const spa_pod* params[1] = { spa_format_audio_raw_build(&builder, SPA_PARAM_EnumFormat, &info) };
    const auto direction = capture ? PW_DIRECTION_INPUT : PW_DIRECTION_OUTPUT;
    const auto flags = pw_stream_flags(PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_RT_PROCESS |
                                       PW_STREAM_FLAG_MAP_BUFFERS);

    uint32_t connect_target = PW_ID_ANY;
    if (!i.target.empty()) {
        std::unique_lock<std::mutex> lock(i.registry_mutex);
        if (!i.registry_cv.wait_for(lock, std::chrono::seconds(2), [&] { return i.target_found; }))
            throw std::runtime_error("PipeWire target not found: " + i.target);
        connect_target = i.target_id;
    }
    pw_thread_loop_lock(i.engine->loop);
    int rc = pw_stream_connect(i.stream, direction, connect_target, flags, params, 1);
    pw_thread_loop_unlock(i.engine->loop);
    if (rc < 0)
        throw std::runtime_error(std::string("PipeWire connect failed: ") + spa_strerror(rc));
    pw_thread_loop_lock(i.engine->loop);
    pw_stream_set_active(i.stream, true);
    i.active.store(true);
    pw_thread_loop_unlock(i.engine->loop);

    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
    while (!i.running.load() && !i.failed.load() && std::chrono::steady_clock::now() < deadline)
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    if (i.failed.load()) i.failure.check();
    if (!i.running.load())
        throw std::runtime_error("PipeWire target did not connect: " + i.target);
}

PipeWirePcm::~PipeWirePcm() {
    Impl& i = *impl_;
    if (i.stream) {
        pw_thread_loop_lock(i.engine->loop);
        pw_stream_set_active(i.stream, false);
        pw_stream_destroy(i.stream);
        pw_thread_loop_unlock(i.engine->loop);
        i.stream = nullptr;
    }
}

void PipeWirePcm::start() {
    Impl& i = *impl_;
    if (i.capture && !i.active.exchange(true)) {
        pw_thread_loop_lock(i.engine->loop);
        pw_stream_set_active(i.stream, true);
        pw_thread_loop_unlock(i.engine->loop);
    }
}

void PipeWirePcm::stop_capture() {
    Impl& i = *impl_;
    if (i.capture && i.active.exchange(false)) {
        pw_thread_loop_lock(i.engine->loop);
        pw_stream_set_active(i.stream, false);
        pw_thread_loop_unlock(i.engine->loop);
    }
}

void PipeWirePcm::set_target(unsigned frames) { impl_->target_frames = frames; }

int64_t PipeWirePcm::delay() {
    return int64_t(impl_->ring.size() / FrameBytes);
}

void PipeWirePcm::wait_target() {
    Impl& i = *impl_;
    if (i.capture) return;
    const size_t limit = size_t(i.target_frames) * i.block * FrameBytes;
    if (!i.ring.wait_low(limit, std::chrono::milliseconds(3 * MOZART_USB_IO_TIMEOUT_MS), i.failure))
        throw std::runtime_error("PipeWire playback target timeout");
}

void PipeWirePcm::transfer(uint8_t* data, unsigned frames) {
    Impl& i = *impl_;
    const size_t bytes = size_t(frames) * FrameBytes;
    if (i.capture) {
        if (!i.ring.pop(data, bytes, std::chrono::milliseconds(3 * MOZART_USB_IO_TIMEOUT_MS), i.failure))
            throw std::runtime_error("PipeWire capture timeout");
        return;
    }
    if (!i.ring.wait_space(bytes, std::chrono::milliseconds(3 * MOZART_USB_IO_TIMEOUT_MS), i.failure))
        throw std::runtime_error("PipeWire playback queue full");
    i.ring.push_playback(data, bytes);
}

void PipeWirePcm::drain() {
    Impl& i = *impl_;
    if (!i.capture && !i.ring.empty(std::chrono::milliseconds(3 * MOZART_USB_IO_TIMEOUT_MS), i.failure))
        throw std::runtime_error("PipeWire playback drain timeout");
}

bool PipeWirePcm::running() { return impl_->running.load(); }

} // namespace mozart_host
