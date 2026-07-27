#include "state/log_store.hpp"

#include <algorithm>
#include <chrono>
#include <ctime>
#include <iomanip>
#include <mutex>
#include <sstream>

#include <spdlog/details/log_msg.h>
#include <spdlog/sinks/base_sink.h>
#include <spdlog/spdlog.h>

namespace rvc {
namespace {

constexpr size_t kMaximumLogEntries = 1000;
std::mutex log_mutex;
std::vector<BackendLogEntry> entries;
bool installed = false;

class ApiLogSink final : public spdlog::sinks::base_sink<std::mutex> {
private:
    void sink_it_(const spdlog::details::log_msg& message) override {
        const auto time = spdlog::log_clock::to_time_t(message.time);
        std::tm local_time{};
#ifdef _WIN32
        localtime_s(&local_time, &time);
#else
        localtime_r(&time, &local_time);
#endif
        std::ostringstream timestamp;
        timestamp << std::put_time(&local_time, "%Y-%m-%dT%H:%M:%S");
        const auto level = spdlog::level::to_string_view(message.level);

        std::lock_guard<std::mutex> lock(log_mutex);
        entries.push_back({timestamp.str(), std::string(level.data(), level.size()),
                           std::string(message.payload.data(), message.payload.size())});
        if (entries.size() > kMaximumLogEntries) entries.erase(entries.begin());
    }

    void flush_() override {}
};

} // namespace

void install_backend_log_sink() {
    std::lock_guard<std::mutex> lock(log_mutex);
    if (installed) return;
    spdlog::default_logger()->sinks().push_back(std::make_shared<ApiLogSink>());
    installed = true;
}

std::vector<BackendLogEntry> recent_backend_logs(size_t limit) {
    std::lock_guard<std::mutex> lock(log_mutex);
    const size_t start = entries.size() > limit ? entries.size() - limit : 0;
    return {entries.begin() + static_cast<std::ptrdiff_t>(start), entries.end()};
}

void clear_backend_logs() {
    std::lock_guard<std::mutex> lock(log_mutex);
    entries.clear();
}

} // namespace rvc
