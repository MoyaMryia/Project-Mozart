// output/pipewire_sink.cpp
// Stub implementation: #ifdef-guarded so unconditional builds (without libpipewire)
// compile cleanly. Defined only under MOZART_ENABLE_PIPEWIRE.
#include "output/pipewire_sink.hpp"
#include "utils/logging.hpp"

namespace mozart {

#ifdef MOZART_ENABLE_PIPEWIRE

#include <spa/param/audio/format-utils.h>
#include <spa/pod/builder.h>
#include <pipewire/pipewire.h>

struct PipeWireSink::Impl {
    pw_loop*      loop        = nullptr;
    pw_context*   context     = nullptr;
    pw_core*      core         = nullptr;
    pw_stream*    stream       = nullptr;
    struct spa_hook stream_listener;
    std::string   source_name_;
    int           sample_rate_;
    int           channels_;
    std::atomic<bool> draining_{false};

    Impl(const std::string& name, int sr, int ch)
        : source_name_(name), sample_rate_(sr), channels_(ch) {}

    ~Impl() {
        if (stream) pw_stream_destroy(stream);
        if (core)   pw_core_disconnect(core);
        if (context) pw_context_destroy(context);
        if (loop)   pw_loop_destroy(loop);
    }
};

// Process callback: we drive this thread from main loop via write().
static void on_process(void* userdata) {
    auto* impl = static_cast<PipeWireSink::Impl*>(userdata);
    // The actual buffer fill happens in write(); PipeWire pulls from our queue.
    // A full impl would keep a lock-free ring here; simplified for skeleton.
    (void)impl;
}

PipeWireSink::PipeWireSink(const std::string& source_name,
                           int sample_rate, int channels)
    : impl_(std::make_unique<Impl>(source_name, sample_rate, channels)) {
    pw_init(nullptr, nullptr);
    impl_->loop = pw_loop_new(nullptr);
    impl_->context = pw_context_new(impl_->loop, nullptr, 0);
    impl_->core = pw_context_connect(impl_->context, nullptr, 0);

    const struct spa_pod* params[1];
    uint8_t buffer[1024];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));

    struct spa_audio_info_raw info = {};
    info.format = SPA_AUDIO_FORMAT_F32;
    info.channels = static_cast<uint32_t>(channels);
    info.rate = static_cast<uint32_t>(sample_rate);

    params[0] = spa_format_audio_raw_build(&b, SPA_PARAM_EnumFormat, &info);

    pw_properties* props = pw_properties_new(
        PW_KEY_MEDIA_TYPE, "Audio",
        PW_KEY_MEDIA_CATEGORY, "Source",
        PW_KEY_MEDIA_ROLE, "Communication",
        PW_KEY_STREAM_NAME, source_name.c_str(),
        PW_KEY_NODE_NAME, source_name.c_str(),
        nullptr);

    impl_->stream = pw_stream_new(impl_->core, "mozart-post-source", props);
    pw_stream_add_listener(impl_->stream, &impl_->stream_listener, on_process, impl_.get());

    pw_stream_connect(impl_->stream,
        PW_STREAM_FLAG_UNBUFFERED | PW_STREAM_FLAG_MAP_BUFFERS,
        PW_DIRECTION_OUTPUT, 1, params);

    MOZART_INFO("PipeWireSink: virtual source '%s' @ %dHz registered",
                source_name.c_str(), sample_rate);
}

PipeWireSink::~PipeWireSink() = default;

void PipeWireSink::write(std::span<const float> /*pcm*/) {
    // Skeleton: actual write requires a process callback + ring buffer mapping.
    // The on_process pulls from there. Given frame-driven model and PipeWire's
    // pull model, a real impl spawns a dedicated pw loop thread.
}

#else

// With PipeWire disabled, this file still compiles into the core lib only if
// CMakeLists added its .cpp under MOZART_ENABLE_PIPEWIRE. Provide a stub
// constructor so tests linking against the lib without the option still link.
struct PipeWireSink::Impl {};

PipeWireSink::PipeWireSink(const std::string& source_name,
                           int sample_rate, int channels) {
    (void)source_name; (void)sample_rate; (void)channels;
    MOZART_ERROR("PipeWireSink built without MOZART_ENABLE_PIPEWIRE");
}

PipeWireSink::~PipeWireSink() = default;
void PipeWireSink::write(std::span<const float>) {
    MOZART_ERROR("PipeWireSink not available in this build");
}

#endif

} // namespace mozart