#pragma once

#include <string>
#include <functional>
#include <memory>
#include <thread>
#include <atomic>
#include <mutex>
#include <vector>
#include <set>
#include <chrono>

#include "state/control_plane.hpp"
#include "mozart/monitor.hpp"

namespace rvc {

// ──────────────────────────────────────────────────────────
// HTTP REST API for model management and service status
// Runs on a dedicated thread alongside the audio worker
// ──────────────────────────────────────────────────────────
class HttpApiServer {
public:
    struct Limits {
        size_t connections = 16;
        size_t subtitle_connections = 8;
        size_t request_memory_bytes = 128ULL * 1024 * 1024;
        std::chrono::milliseconds header_timeout{5000};
        std::chrono::milliseconds request_timeout{30000};
        std::chrono::milliseconds write_timeout{30000};
    };

    HttpApiServer(
        const std::string& host,
        uint16_t port,
        ControlPlane* controller
    );
    HttpApiServer(const std::string& host, uint16_t port, ControlPlane* controller, Limits limits);

    ~HttpApiServer();

    bool start();
    void stop();

private:
    std::string host_;
    std::string stack_status_path_;
    std::string glossary_path_;
    std::string stack_instance_id_;
    uint16_t port_;
    ControlPlane* controller_;
    SystemMonitor monitor_;
    std::mutex monitor_mutex_;
    Limits limits_;

    std::atomic<bool> running_{false};
    std::thread server_thread_;
    int server_fd_ = -1;

    // 所有连接均受数量限制。停止时关闭活动 socket，再等待线程退出。
    struct Worker {
        std::thread thread;
        std::shared_ptr<std::atomic<bool>> done;
    };
    std::mutex workers_mutex_;
    std::vector<Worker> workers_;
    std::set<int> clients_;
    std::atomic<size_t> subtitle_connections_{0};
    std::atomic<size_t> request_bytes_{0};

    void run_server();
    void handle_request(int client_fd);
    void reap_finished_workers_locked();

    // Route handlers
    std::string handle_health();
    std::string handle_status();
    std::string handle_stack_status();
    std::string handle_monitor();
    std::string handle_logs(const std::string& path);
    std::string handle_logs_clear();
    std::string handle_parameters_get();
    std::string handle_parameters_set(const std::string& body);
    std::string handle_parameters_reset();
    std::string handle_glossary_get();
    std::string handle_glossary_set(const std::string& body);
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
