#pragma once
#include "source_base.hpp"
#include <string>

namespace mozart {

// Ring buffer source via dlopen (local shared memory transport)
// Loads libmozart_pre.so and calls mozart_post_attach/poll/detach
class RingSource : public SourceBase {
public:
    explicit RingSource(const std::string& ring_name);
    ~RingSource();

    bool poll(FrameBuf& out_frame, int timeout_us = 100000) override;
    const char* name() const override { return "ring"; }

private:
    void* handle_ = nullptr;
    void* ctx_ = nullptr;

    // Function pointers from dlopen
    using AttachFn = void* (*)(const char*);
    using PollFn = int (*)(void*, float*, FrameMeta*, int);
    using DetachFn = void (*)(void*);

    AttachFn attach_fn_ = nullptr;
    PollFn poll_fn_ = nullptr;
    DetachFn detach_fn_ = nullptr;
};

}  // namespace mozart
