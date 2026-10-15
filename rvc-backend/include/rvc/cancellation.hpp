#pragma once

#include <atomic>
#include <stdexcept>

namespace rvc {

class InferenceCancelled final : public std::runtime_error {
public:
    InferenceCancelled() : std::runtime_error("RVC inference cancelled") {}
};

inline void check_cancelled(const std::atomic<bool>* cancelled) {
    if (cancelled && cancelled->load(std::memory_order_relaxed)) {
        throw InferenceCancelled();
    }
}

} // namespace rvc
