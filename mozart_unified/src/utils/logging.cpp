#include <mozart/utils/logging.hpp>
#include <spdlog/sinks/stdout_color_sinks.h>

namespace mozart {

static std::shared_ptr<spdlog::logger> g_logger;

void init_logging(const std::string& level) {
    if (!g_logger) {
        g_logger = spdlog::stdout_color_mt("mozart");
    }

    if (level == "trace") g_logger->set_level(spdlog::level::trace);
    else if (level == "debug") g_logger->set_level(spdlog::level::debug);
    else if (level == "info") g_logger->set_level(spdlog::level::info);
    else if (level == "warn") g_logger->set_level(spdlog::level::warn);
    else if (level == "error") g_logger->set_level(spdlog::level::err);
    else if (level == "critical") g_logger->set_level(spdlog::level::critical);
    else g_logger->set_level(spdlog::level::info);

    g_logger->set_pattern("[%Y-%m-%d %H:%M:%S.%e] [%^%l%$] [%n] %v");
}

std::shared_ptr<spdlog::logger> get_logger() {
    if (!g_logger) {
        init_logging("info");
    }
    return g_logger;
}

}  // namespace mozart
