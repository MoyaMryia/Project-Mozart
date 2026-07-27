#pragma once

#include <cstdint>

#include <nlohmann/json.hpp>

namespace rvc {

// Collects host metrics locally so the web client never has to guess hardware state.
class SystemMonitor final {
public:
    nlohmann::json snapshot();

private:
    uint64_t previous_cpu_total_ = 0;
    uint64_t previous_cpu_idle_ = 0;
};

} // namespace rvc
