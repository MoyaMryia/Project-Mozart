#include <mozart/output/dummy_sink.hpp>

namespace mozart {

void DummySink::write(const OutputFrameBuf&) {
    // Discard output (for testing)
}

}  // namespace mozart
