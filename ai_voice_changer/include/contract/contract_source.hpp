// contract/contract_source.hpp
// Abstract source of preprocessed frames crossing the Mozart contract boundary.
#pragma once

#include "frame_meta.hpp"

#include <array>
#include <cstddef>

namespace mozart {

// Frame size at the contract rate: 16kHz * 20ms = 320 samples.
inline constexpr std::size_t kContractSamples = 320;
using ContractFrame = std::array<float, kContractSamples>;

// Poll result codes.
enum class PollResult {
    Timeout = 0,   // no frame within timeout
    Ok      = 1,   // frame + meta written
    Error   = -1,  // fatal, caller should stop
};

class ContractSource {
public:
    virtual ~ContractSource() = default;

    // Block up to timeout_us for one frame. Default timeout if <=0.
    virtual PollResult poll(ContractFrame& out_pcm, FrameMeta& out_meta,
                            int timeout_us) = 0;

    // Human-readable name for logging/status.
    virtual const char* name() const = 0;
};

} // namespace mozart