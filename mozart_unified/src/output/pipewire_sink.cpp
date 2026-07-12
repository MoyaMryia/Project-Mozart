#include <mozart/output/pipewire_sink.hpp>
#include <mozart/utils/logging.hpp>

#ifdef MOZART_ENABLE_PIPEWIRE
#include <pipewire/pipewire.h>
#endif

namespace mozart {

#ifdef MOZART_ENABLE_PIPEWIRE

struct PipeWireSink::Impl {
    pw_loop* loop = nullptr;
    pw_context* context = nullptr;
    pw_core* core = nullptr;
    pw_stream* stream = nullptr;
    Config cfg;
};

PipeWireSink::PipeWireSink(const Config& cfg) : impl_(std::make_unique<Impl>()) {
    impl_->cfg = cfg;

    pw_init(nullptr, nullptr);

    impl_->loop = pw_loop_new(nullptr);
    impl_->context = pw_context_new(impl_->loop, nullptr, 0);
    impl_->core = pw_context_connect(impl_->context, nullptr, 0);

    // Create stream
    pw_stream_events events{};
    events.version = PW_VERSION_STREAM_EVENTS;

    impl_->stream = pw_stream_new_simple(
        impl_->core,
        cfg.node_name.c_str(),
        pw_properties_new(
            PW_KEY_MEDIA_TYPE, "Audio",
            PW_KEY_MEDIA_CATEGORY, "Capture",
            PW_KEY_MEDIA_ROLE, "Music",
            PW_KEY_NODE_NAME, cfg.node_name.c_str(),
            PW_KEY_NODE_DESCRIPTION, cfg.node_description.c_str(),
            nullptr),
        &events,
        nullptr
    );

    // Set format
    const spa_pod* params[1];
    uint8_t buffer[1024];
    spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));

    params[0] = static_cast<const spa_pod*>(
        spa_pod_builder_add_object(&b,
            SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
            SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_audio),
            SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
            SPA_FORMAT_AUDIO_format, SPA_POD_Id(SPA_AUDIO_FORMAT_F32),
            SPA_FORMAT_AUDIO_rate, SPA_POD_Int(48000),
            SPA_FORMAT_AUDIO_channels, SPA_POD_Int(1)
        )
    );

    pw_stream_connect(impl_->stream,
                      PW_DIRECTION_OUTPUT,
                      PW_ID_ANY,
                      static_cast<pw_stream_flags>(PW_STREAM_FLAG_AUTOCONNECT |
                                                   PW_STREAM_FLAG_MAP_BUFFERS),
                      params, 1);

    MOZART_LOG_INFO("PipeWire virtual source created: {}", cfg.node_name);
}

PipeWireSink::~PipeWireSink() {
    if (impl_->stream) pw_stream_destroy(impl_->stream);
    if (impl_->core) pw_core_disconnect(impl_->core);
    if (impl_->context) pw_context_destroy(impl_->context);
    if (impl_->loop) pw_loop_destroy(impl_->loop);
    pw_deinit();
}

void PipeWireSink::write(const OutputFrameBuf& frame) {
    // TODO: Implement actual PipeWire buffer writing
    // This requires proper buffer management and spa_pod manipulation
    MOZART_LOG_TRACE("PipeWire write: {} samples", frame.samples.size());
}

#else  // MOZART_ENABLE_PIPEWIRE

struct PipeWireSink::Impl {};

PipeWireSink::PipeWireSink(const Config&) : impl_(std::make_unique<Impl>()) {
    MOZART_LOG_WARN("PipeWire not enabled, sink will not work");
}

PipeWireSink::~PipeWireSink() = default;

void PipeWireSink::write(const OutputFrameBuf&) {
    // No-op
}

#endif  // MOZART_ENABLE_PIPEWIRE

}  // namespace mozart
