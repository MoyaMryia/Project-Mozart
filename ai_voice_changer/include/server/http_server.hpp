// server/http_server.hpp
// cpp-httplib-based HTTP REST server for model management. Requires MOZART_ENABLE_HTTP.
#pragma once

#include "rvc/model_loader.hpp"

#include <atomic>
#include <memory>
#include <string>
#include <thread>

namespace mozart {

struct PipelineStats {
    std::atomic<uint64_t> frames_processed{0};
    std::atomic<uint64_t> frames_silence{0};
    std::atomic<double>   last_latency_ms{0.0};
    std::atomic<double>   avg_latency_ms{0.0};
};

// Runs the HTTP API in a background thread. Construction opens the socket.
// destroy() / dtor signals shutdown and joins.
class HttpServer {
public:
    HttpServer(const std::string& host,
               int port,
               ModelManager& models,
               PipelineStats& stats,
               const std::string& mode_name);
    ~HttpServer();

    // Sync: blocks until the server has bound its port (ready for requests).
    void wait_ready();

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace mozart