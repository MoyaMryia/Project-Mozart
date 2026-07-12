#include <mozart/source/ring_source.hpp>
#include <mozart/utils/logging.hpp>
#include <dlfcn.h>
#include <stdexcept>

namespace mozart {

RingSource::RingSource(const std::string& ring_name) {
    // Try to load the preprocessing library
    const char* lib_names[] = {"libmozart_pre.so", "mozart_pre.so", "mozart_pre"};
    for (const char* name : lib_names) {
        handle_ = dlopen(name, RTLD_NOW);
        if (handle_) {
            MOZART_LOG_INFO("Loaded preprocessing library: {}", name);
            break;
        }
    }

    if (!handle_) {
        throw std::runtime_error("Failed to load mozart_pre library: " + std::string(dlerror()));
    }

    // Resolve symbols
    attach_fn_ = reinterpret_cast<AttachFn>(dlsym(handle_, "mozart_post_attach"));
    poll_fn_ = reinterpret_cast<PollFn>(dlsym(handle_, "mozart_post_poll"));
    detach_fn_ = reinterpret_cast<DetachFn>(dlsym(handle_, "mozart_post_detach"));

    if (!attach_fn_ || !poll_fn_ || !detach_fn_) {
        dlclose(handle_);
        throw std::runtime_error("Failed to resolve mozart_post_* symbols");
    }

    // Attach to ring buffer
    ctx_ = attach_fn_(ring_name.c_str());
    if (!ctx_) {
        dlclose(handle_);
        throw std::runtime_error("mozart_post_attach returned NULL for: " + ring_name);
    }

    MOZART_LOG_INFO("Attached to ring buffer: {}", ring_name);
}

RingSource::~RingSource() {
    if (ctx_ && detach_fn_) {
        detach_fn_(ctx_);
    }
    if (handle_) {
        dlclose(handle_);
    }
}

bool RingSource::poll(FrameBuf& out_frame, int timeout_us) {
    if (!ctx_ || !poll_fn_) return false;

    int result = poll_fn_(ctx_, out_frame.samples.data(), &out_frame.meta, timeout_us);
    return result == 1;
}

}  // namespace mozart
