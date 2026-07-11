// contract/mozart_ring_source.hpp
// Contract source backed by the Mozart preprocessing shared ring via the
// mozart_post_* C ABI. Links dynamically (dlopen) so the binary can run without
// the upstream library present; construction then reports a clear error.
#pragma once

#include "contract/contract_source.hpp"
#include "mozart_post.h"

#include <memory>
#include <string>

namespace mozart {

class MozartRingSource final : public ContractSource {
public:
    // Open the shared ring. Throws std::runtime_error if the mozart_pre
    // library cannot be located or the ring cannot be attached.
    explicit MozartRingSource(const std::string& ring_name);
    ~MozartRingSource() override;

    MozartRingSource(const MozartRingSource&) = delete;
    MozartRingSource& operator=(const MozartRingSource&) = delete;

    PollResult poll(ContractFrame& out_pcm, FrameMeta& out_meta,
                    int timeout_us) override;

    const char* name() const override { return "mozart-ring"; }

private:
    void*                          dll_handle_ = nullptr;
    mozart_post*                   handle_     = nullptr;
    mozart_post* (*fn_attach_)(const char*)                                = nullptr;
    int          (*fn_poll_)(mozart_post*, float*, mozart_frame_meta*, int) = nullptr;
    void         (*fn_detach_)(mozart_post*)                               = nullptr;
};

} // namespace mozart