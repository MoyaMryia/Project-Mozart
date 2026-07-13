#pragma once

#include <string>
#include <functional>
#include <memory>
#include <thread>
#include <atomic>

#include "rvc/pipeline.hpp"
#include "rvc/audio_worker.hpp"

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
        RVCPipelineBase* pipeline,
        AudioWorker* audio_worker,
        const std::string& models_dir
    );

    ~HttpApiServer();

    void start();
    void stop();

private:
    std::string host_;
    uint16_t port_;
    RVCPipelineBase* pipeline_;
    AudioWorker* audio_worker_;
    std::string models_dir_;

    std::atomic<bool> running_{false};
    std::thread server_thread_;
    int server_fd_ = -1;

    void run_server();
    void handle_request(int client_fd);

    // Route handlers
    std::string handle_health();
    std::string handle_status();
    std::string handle_list_models();
    std::string handle_upload_model(int client_fd, const std::string& header);
    std::string handle_activate_model(const std::string& model_id);
};

} // namespace rvc
