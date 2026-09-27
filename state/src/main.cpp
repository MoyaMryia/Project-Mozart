#include <atomic>
#include <chrono>
#include <csignal>
#include <iostream>
#include <thread>

#include <spdlog/sinks/stdout_color_sinks.h>
#include <spdlog/spdlog.h>

#include "mozart/state_daemon.hpp"
#include "state/log_store.hpp"
#include "utils/config.hpp"

namespace {

std::atomic<bool> shutdown_requested{false};

void signal_handler(int signal) {
    (void)signal;
    // 信号处理器内只置位：spdlog 会加锁/分配，不是 async-signal-safe。
    shutdown_requested.store(true);
}

} // namespace

int main(int argc, char* argv[]) {
    auto console = spdlog::stdout_color_mt("console");
    console->set_level(spdlog::level::info);
    console->set_pattern("%Y-%m-%d %H:%M:%S.%e [%^%l%$] %v");
    spdlog::set_default_logger(console);
    rvc::install_backend_log_sink();

    rvc::Config config;
    try {
        config = argc > 1 ? rvc::Config::from_yaml(argv[1]) : rvc::Config::default_config();
    } catch (const std::exception& error) {
        spdlog::error("Failed to load daemon configuration: {}", error.what());
        return 1;
    }

    mozart::StateManagerDaemon daemon(std::move(config));
    std::signal(SIGINT, signal_handler);
    std::signal(SIGTERM, signal_handler);
#ifndef _WIN32
    // 客户端中断下载不应让进程收到 SIGPIPE。
    std::signal(SIGPIPE, SIG_IGN);
#endif
    bool started = false;
    try {
        started = daemon.start();
    } catch (const std::exception& error) {
        spdlog::error("State manager failed to start: {}", error.what());
        return 1;
    }
    if (!started) {
        spdlog::error("State manager could not start its HTTP control plane");
        return 1;
    }

    while (!shutdown_requested.load()) std::this_thread::sleep_for(std::chrono::milliseconds(100));
    daemon.stop();
    return 0;
}
