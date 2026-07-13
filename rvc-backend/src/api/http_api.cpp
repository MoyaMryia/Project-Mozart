#include "api/http_api.hpp"
#include "common.hpp"
#include <spdlog/spdlog.h>
#include <nlohmann/json.hpp>
#include <sstream>
#include <cstring>

namespace rvc {

// ──────────────────────────────────────────────────────────
// Simple HTTP helpers
// ──────────────────────────────────────────────────────────
static std::string http_response(int code, const std::string& body, const std::string& content_type = "application/json") {
    std::string status;
    switch (code) {
        case 200: status = "200 OK"; break;
        case 400: status = "400 Bad Request"; break;
        case 404: status = "404 Not Found"; break;
        case 500: status = "500 Internal Server Error"; break;
        default:  status = "200 OK"; break;
    }

    return "HTTP/1.1 " + status + "\r\n"
           "Content-Type: " + content_type + "\r\n"
           "Content-Length: " + std::to_string(body.size()) + "\r\n"
           "Connection: close\r\n"
           "\r\n" + body;
}

static std::string parse_request_path(const std::string& req) {
    size_t space1 = req.find(' ');
    if (space1 == std::string::npos) return "/";
    size_t space2 = req.find(' ', space1 + 1);
    if (space2 == std::string::npos) return "/";
    return req.substr(space1 + 1, space2 - space1 - 1);
}

static std::string parse_request_method(const std::string& req) {
    size_t space = req.find(' ');
    if (space == std::string::npos) return "GET";
    return req.substr(0, space);
}

// ──────────────────────────────────────────────────────────
// HttpApiServer
// ──────────────────────────────────────────────────────────
HttpApiServer::HttpApiServer(
    const std::string& host,
    uint16_t port,
    RVCPipelineBase* pipeline,
    AudioWorker* audio_worker,
    const std::string& models_dir
)
    : host_(host), port_(port)
    , pipeline_(pipeline)
    , audio_worker_(audio_worker)
    , models_dir_(models_dir)
{}

HttpApiServer::~HttpApiServer() {
    stop();
}

void HttpApiServer::start() {
    server_fd_ = socket(AF_INET, SOCK_STREAM, 0);
    if (server_fd_ < 0) {
        spdlog::error("Failed to create HTTP socket");
        return;
    }

    int opt = 1;
    setsockopt(server_fd_, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port_);
    inet_pton(AF_INET, host_.c_str(), &addr.sin_addr);

    if (bind(server_fd_, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) < 0) {
        spdlog::error("Failed to bind HTTP socket to {}:{}", host_, port_);
        socket_close(server_fd_);
        return;
    }

    if (listen(server_fd_, 10) < 0) {
        spdlog::error("Failed to listen on HTTP socket");
        socket_close(server_fd_);
        return;
    }

    running_ = true;
    server_thread_ = std::thread(&HttpApiServer::run_server, this);

    spdlog::info("HTTP API server listening on {}:{}", host_, port_);
}

void HttpApiServer::stop() {
    running_ = false;
    if (server_fd_ >= 0) {
#ifdef _WIN32
        shutdown(server_fd_, SD_BOTH);
#else
        shutdown(server_fd_, SHUT_RDWR);
#endif
        socket_close(server_fd_);
        server_fd_ = -1;
    }
    if (server_thread_.joinable()) {
        server_thread_.join();
    }
}

void HttpApiServer::run_server() {
    while (running_) {
        struct sockaddr_in client_addr;
        socklen_t client_len = sizeof(client_addr);

        int client_fd = accept(server_fd_,
            reinterpret_cast<struct sockaddr*>(&client_addr), &client_len);

        if (client_fd < 0) {
            if (!running_) break;
            continue;
        }

        handle_request(client_fd);
        socket_close(client_fd);
    }
}

void HttpApiServer::handle_request(int client_fd) {
    char buffer[4096];
    ssize_t n = recv(client_fd, buffer, sizeof(buffer) - 1, 0);
    if (n <= 0) return;
    buffer[n] = '\0';

    std::string request(buffer, static_cast<size_t>(n));
    std::string method = parse_request_method(request);
    std::string path = parse_request_path(request);

    std::string response;

    if (path == "/health" && method == "GET") {
        response = handle_health();
    }
    else if (path == "/status" && method == "GET") {
        response = handle_status();
    }
    else if (path == "/models" && method == "GET") {
        response = handle_list_models();
    }
    else if (path.find("/models/") == 0 && path.find("/activate") != std::string::npos && method == "POST") {
        // Extract model_id from /models/{id}/activate
        size_t start = 8; // after "/models/"
        size_t end = path.find("/activate", start);
        std::string model_id = path.substr(start, end - start);
        response = handle_activate_model(model_id);
    }
    else {
        response = http_response(404, R"({"error":"not found"})");
    }

    send(client_fd, response.c_str(), response.size(), 0);
}

std::string HttpApiServer::handle_health() {
    return http_response(200, R"({"status":"ok"})");
}

std::string HttpApiServer::handle_status() {
    auto latency = audio_worker_->get_latency_stats();
    auto bypass = audio_worker_->get_bypass_stats();
    const auto& config = audio_worker_->config();

    nlohmann::json j;
    j["mode"] = "real"; // Could introspect pipeline type
    j["model"]["current_model_id"] = nullptr;
    j["model"]["loaded"] = false;
    j["latency_stats_ms"]["count"] = latency.count;
    j["latency_stats_ms"]["avg_ms"] = latency.avg_ms;
    j["latency_stats_ms"]["max_ms"] = latency.max_ms;
    j["bypass_stats"] = {
        {"inference_count", bypass.inference_count},
        {"bypass_count", bypass.bypass_count}
    };
    j["contract_config"] = {
        {"host", config.host},
        {"port", config.port},
        {"input_sample_rate_hz", config.input_sample_rate},
        {"output_sample_rate_hz", config.output_sample_rate},
        {"frame_duration_ms", config.frame_duration_ms},
        {"input_samples_per_frame", MOZART_INPUT_SAMPLES},
        {"output_samples_per_frame", MOZART_OUTPUT_SAMPLES}
    };

    return http_response(200, j.dump(2));
}

std::string HttpApiServer::handle_list_models() {
    // Simple directory scan for now
    nlohmann::json j;
    j["models"] = nlohmann::json::array();

    std::filesystem::path dir(models_dir_);
    if (std::filesystem::exists(dir)) {
        for (const auto& entry : std::filesystem::directory_iterator(dir)) {
            if (!entry.is_directory()) continue;
            std::string name = entry.path().filename().string();
            bool has_pth = std::filesystem::exists(entry.path() / (name + ".pth"));
            bool has_cfg = std::filesystem::exists(entry.path() / "config.json");

            j["models"].push_back({
                {"id", name},
                {"exists", has_pth && has_cfg},
                {"loaded", false},
                {"current", false}
            });
        }
    }

    return http_response(200, j.dump(2));
}

std::string HttpApiServer::handle_upload_model(int client_fd, const std::string& header) {
    // TODO: Implement multipart form parsing
    return http_response(501, R"({"error":"upload not implemented yet"})");
}

std::string HttpApiServer::handle_activate_model(const std::string& model_id) {
    // TODO: Implement model activation when RealRVCPipeline is connected
    spdlog::info("Activate model request: {}", model_id);
    nlohmann::json j;
    j["status"] = "activated";
    j["model_id"] = model_id;
    return http_response(200, j.dump());
}

} // namespace rvc
