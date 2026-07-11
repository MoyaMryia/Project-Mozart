// output/dummy_sink.cpp
#include "output/dummy_sink.hpp"

namespace mozart {

void DummySink::write(std::span<const float> /*pcm*/) {
    frames_.fetch_add(1, std::memory_order_relaxed);
}

} // namespace mozart