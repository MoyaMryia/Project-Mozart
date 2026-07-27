#pragma once

#include <cstddef>
#include <string>
#include <vector>

namespace rvc {

struct BackendLogEntry {
    std::string timestamp;
    std::string level;
    std::string message;
};

// Retains the daemon's existing spdlog events for transport adapters. It does
// not create a second application log stream.
void install_backend_log_sink();
std::vector<BackendLogEntry> recent_backend_logs(size_t limit = 200);
void clear_backend_logs();

} // namespace rvc
