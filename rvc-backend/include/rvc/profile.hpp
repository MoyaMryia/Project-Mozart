#pragma once

#include <chrono>
#include <cstdlib>

namespace rvc::profile {

using Clock = std::chrono::steady_clock;

inline bool enabled() {
    static const bool value = [] {
        const char* setting = std::getenv("MOZART_RVC_PROFILE");
        return setting && setting[0] != '\0' && setting[0] != '0';
    }();
    return value;
}

inline double elapsed_ms(Clock::time_point begin, Clock::time_point end) {
    return std::chrono::duration<double, std::milli>(end - begin).count();
}

} // namespace rvc::profile
