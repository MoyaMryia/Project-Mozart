#pragma once
#include <string>
#include <functional>
#include <memory>

namespace mozart {

// HTTP API server for model management and status
class HttpServer {
public:
    struct Callbacks {
        std::function<std::string()> on_health;
        std::function<std::string()> on_status;
        std::function<std::string()> on_list_models;
        std::function<bool(const std::string&)> on_activate_model;
        std::function<bool(const std::string&, const std::vector<uint8_t>&)> on_upload_model;
    };

    struct Config {
        std::string bind_addr = "0.0.0.0";
        uint16_t port = 18080;
    };

    HttpServer(const Config& cfg, const Callbacks& cbs);
    ~HttpServer();

    void start();
    void stop();

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace mozart
