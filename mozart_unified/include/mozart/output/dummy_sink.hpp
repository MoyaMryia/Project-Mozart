#pragma once
#include "sink_base.hpp"

namespace mozart {

// Dummy sink that discards output (for testing)
class DummySink : public SinkBase {
public:
    void write(const OutputFrameBuf& frame) override;
    const char* name() const override { return "dummy"; }
};

}  // namespace mozart
