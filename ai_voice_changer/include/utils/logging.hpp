// utils/logging.hpp
// Minimal leveled logging used across the project.
#pragma once

#include <cstdio>

namespace mozart {

enum class LogLevel { Debug, Info, Warn, Error };

#ifndef MOZART_LOG_LEVEL
#define MOZART_LOG_LEVEL 1  // Info
#endif

namespace detail {
inline const char* level_name(LogLevel l) {
    switch (l) {
        case LogLevel::Debug: return "DEBUG";
        case LogLevel::Info:  return "INFO";
        case LogLevel::Warn:  return "WARN";
        case LogLevel::Error: return "ERROR";
    }
    return "?";
}

inline int level_value(LogLevel l) {
    switch (l) {
        case LogLevel::Debug: return 0;
        case LogLevel::Info:  return 1;
        case LogLevel::Warn:  return 2;
        case LogLevel::Error: return 3;
    }
    return 4;
}
} // namespace detail

#define MOZART_LOG(lvl, ...)                                                       \
    do {                                                                           \
        if (::mozart::detail::level_value(lvl) >= MOZART_LOG_LEVEL) {              \
            std::fprintf(stderr, "[%s] ", ::mozart::detail::level_name(lvl));      \
            std::fprintf(stderr, __VA_ARGS__);                                     \
            std::fputc('\n', stderr);                                              \
        }                                                                          \
    } while (0)

#define MOZART_INFO(...)  MOZART_LOG(::mozart::LogLevel::Info,  __VA_ARGS__)
#define MOZART_WARN(...)  MOZART_LOG(::mozart::LogLevel::Warn,  __VA_ARGS__)
#define MOZART_ERROR(...) MOZART_LOG(::mozart::LogLevel::Error, __VA_ARGS__)
#define MOZART_DEBUG(...) MOZART_LOG(::mozart::LogLevel::Debug, __VA_ARGS__)

} // namespace mozart