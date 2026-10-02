#pragma once

#include <string>
#include <functional>
#include <memory>
#include <thread>
#include <atomic>
#include <mutex>
#include <vector>

#include "state/control_plane.hpp"
#include "mozart/monitor.hpp"

namespace rvc {

// ──────────────────────────────────────────────────────────
// HTTP REST API for model management and service status
// Runs on a dedicated thread alongside the audio worker
// ──────────────────────────────────────────────────────────
class HttpApiServer {
public:
    HttpApiServer(
        const std::string& host,
        uint16_t port,
        ControlPlane* controller
    );

    ~HttpApiServer();

    bool start();
    void stop();

private:
    std::string host_;
    uint16_t port_;
    ControlPlane* controller_;
    SystemMonitor monitor_;

    std::atomic<bool> running_{false};
    std::thread server_thread_;
    int server_fd_ = -1;

    // SSE 连接各自一个工作线程。用 done 标记回收已结束的线程，
    // stop() 统一 join，避免 detach 线程在对象析构后访问 this。
    struct SseWorker {
        std::thread thread;
        std::shared_ptr<std::atomic<bool>> done;
    };
    std::mutex sse_mutex_;
    std::vector<SseWorker> sse_workers_;

    void run_server();
    void handle_request(int client_fd);
    void reap_finished_sse_locked();

    // Route handlers
    std::string handle_health();
    std::string handle_status();
    std::string handle_monitor();
    std::string handle_logs(const std::string& path);
    std::string handle_logs_clear();
    std::string handle_parameters_get();
    std::string handle_parameters_set(const std::string& body);
    std::string handle_parameters_reset();
    std::string handle_presets_get();
    std::string handle_presets_post(const std::string& body);
    std::string handle_presets_delete(const std::string& path);
    std::string handle_list_models();
    std::string handle_mode_switch(const std::string& body);
    std::string handle_realtime_routing(const std::string& body);
    std::string handle_file_upload(const std::string& header, const std::string& body);
    std::string handle_file_status(const std::string& path);
    std::string handle_file_cancel(const std::string& path);
    std::string handle_file_pause();
    std::string handle_file_resume();
    std::string handle_file_remove(const std::string& path);
    std::string handle_file_clear_finished();
    std::string handle_file_result(int client_fd, const std::string& path);
    void handle_subtitles_stream(int client_fd);
    std::string handle_activate_model(const std::string& model_id);
};

} // namespace rvc
