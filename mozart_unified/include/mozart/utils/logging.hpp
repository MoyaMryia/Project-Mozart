#pragma once
#include <spdlog/spdlog.h>

namespace mozart {

// Initialize logging with given level
void init_logging(const std::string& level = "info");

// Get the root logger
std::shared_ptr<spdlog::logger> get_logger();

}  // namespace mozart

// Convenience macros
#define MOZART_LOG_TRACE(...)    mozart::get_logger()->trace(__VA_ARGS__)
#define MOZART_LOG_DEBUG(...)    mozart::get_logger()->debug(__VA_ARGS__)
#define MOZART_LOG_INFO(...)     mozart::get_logger()->info(__VA_ARGS__)
#define MOZART_LOG_WARN(...)     mozart::get_logger()->warn(__VA_ARGS__)
#define MOZART_LOG_ERROR(...)    mozart::get_logger()->error(__VA_ARGS__)
#define MOZART_LOG_CRITICAL(...) mozart::get_logger()->critical(__VA_ARGS__)
