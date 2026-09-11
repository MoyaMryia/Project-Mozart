#include "mozart/http_api.hpp"
#include "common.hpp"
#include "state/log_store.hpp"
#include <spdlog/spdlog.h>
#include <nlohmann/json.hpp>
#include <sstream>
#include <cstring>
#include <fstream>
#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdlib>
#include <limits>
#include <system_error>
#include <thread>
#include <unistd.h>

// 写已断开的 socket 不应让进程收到 SIGPIPE（Linux）。没有该标志时退化为 0。
#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif

namespace rvc {

static std::string http_response(int code, const std::string& body, const std::string& content_type = "application/json") {
    std::string status;
    switch (code) {
        case 200: status = "200 OK"; break;
        case 400: status = "400 Bad Request"; break;
        case 404: status = "404 Not Found"; break;
        case 409: status = "409 Conflict"; break;
        case 413: status = "413 Payload Too Large"; break;
        case 422: status = "422 Unprocessable Content"; break;
        case 501: status = "501 Not Implemented"; break;
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

static std::string query_value(const std::string& path, const std::string& key) {
    const auto start = path.find('?');
    if (start == std::string::npos) return "";
    const std::string prefix = key + "=";
    size_t position = start + 1;
    while (position < path.size()) {
        const auto next = path.find('&', position);
        const size_t end = next == std::string::npos ? path.size() : next;
        if (path.compare(position, prefix.size(), prefix) == 0) {
            return path.substr(position + prefix.size(), end - position - prefix.size());
        }
        if (next == std::string::npos) break;
        position = next + 1;
    }
    return "";
}

static size_t content_length(const std::string& header) {
    std::string normalized = header;
    std::transform(normalized.begin(), normalized.end(), normalized.begin(), [](unsigned char character) {
        return static_cast<char>(std::tolower(character));
    });
    const std::string marker = "\r\ncontent-length:";
    const auto position = normalized.find(marker);
    if (position == std::string::npos) return 0;
    size_t index = position + marker.size();
    const auto value_end = header.find("\r\n", index);
    if (value_end == std::string::npos) return 0;
    while (index < value_end && (header[index] == ' ' || header[index] == '\t')) ++index;
    if (index == value_end) return 0;
    size_t value = 0;
    for (; index < value_end; ++index) {
        const unsigned char character = static_cast<unsigned char>(header[index]);
        if (!std::isdigit(character)) return 0;
        if (value > (std::numeric_limits<size_t>::max() - 9) / 10) return 0;
        value = value * 10 + static_cast<size_t>(character - '0');
    }
    return value;
}

// 音色 ID 只允许单层目录名，阻止 ../ 或绝对路径穿透 models_dir。
static bool valid_model_id(const std::string& model_id) {
    if (model_id.empty() || model_id.size() > 128) return false;
    if (model_id == "." || model_id == "..") return false;
    for (const unsigned char character : model_id) {
        if (!(std::isalnum(character) || character == '_' || character == '-' || character == '.')) {
            return false;
        }
    }
    return model_id.find("..") == std::string::npos;
}

static std::string multipart_value(const std::string& body, const std::string& boundary,
                                   const std::string& field) {
    const std::string marker = "name=\"" + field + "\"";
    const auto field_position = body.find(marker);
    if (field_position == std::string::npos) return "";
    const auto data_start_marker = body.find("\r\n\r\n", field_position);
    if (data_start_marker == std::string::npos) return "";
    const auto data_start = data_start_marker + 4;
    const auto data_end = body.find("\r\n--" + boundary, data_start);
    return data_end == std::string::npos ? "" : body.substr(data_start, data_end - data_start);
}

HttpApiServer::HttpApiServer(
    const std::string& host,
    uint16_t port,
    ControlPlane* controller
)
    : host_(host), port_(port)
    , controller_(controller)
{}

HttpApiServer::~HttpApiServer() {
    stop();
}

bool HttpApiServer::start() {
    if (running_.load()) return false;

    server_fd_ = socket(AF_INET, SOCK_STREAM, 0);
    if (server_fd_ < 0) {
        spdlog::error("Failed to create HTTP socket");
        return false;
    }

    int opt = 1;
    setsockopt(server_fd_, SOL_SOCKET, SO_REUSEADDR,
               reinterpret_cast<const char*>(&opt), sizeof(opt));

    struct sockaddr_in addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port_);
    if (inet_pton(AF_INET, host_.c_str(), &addr.sin_addr) != 1) {
        spdlog::error("Invalid HTTP bind address: {}", host_);
        socket_close(server_fd_);
        server_fd_ = -1;
        return false;
    }

    if (bind(server_fd_, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) < 0) {
        spdlog::error("Failed to bind HTTP socket to {}:{}", host_, port_);
        socket_close(server_fd_);
        return false;
    }

    if (listen(server_fd_, 10) < 0) {
        spdlog::error("Failed to listen on HTTP socket");
        socket_close(server_fd_);
        return false;
    }

    running_ = true;
    server_thread_ = std::thread(&HttpApiServer::run_server, this);

    spdlog::info("HTTP API server listening on {}:{}", host_, port_);
    return true;
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
    std::vector<SseWorker> workers;
    {
        std::lock_guard<std::mutex> lock(sse_mutex_);
        workers.swap(sse_workers_);
    }
    for (auto& worker : workers) {
        if (worker.thread.joinable()) worker.thread.join();
    }
}

void HttpApiServer::reap_finished_sse_locked() {
    auto worker = sse_workers_.begin();
    while (worker != sse_workers_.end()) {
        if (worker->done->load()) {
            if (worker->thread.joinable()) worker->thread.join();
            worker = sse_workers_.erase(worker);
        } else {
            ++worker;
        }
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

        try {
            handle_request(client_fd);
        } catch (const std::exception& error) {
            spdlog::error("HTTP request handling failed: {}", error.what());
            const auto response = http_response(500, R"({"error":"internal server error"})");
            send(client_fd, response.c_str(), response.size(), MSG_NOSIGNAL);
        }
        socket_close(client_fd);
    }
}

void HttpApiServer::handle_request(int client_fd) {
    constexpr size_t max_request_bytes = 110ULL * 1024ULL * 1024ULL;
    std::string request;
    char buffer[8192];
    size_t header_end = std::string::npos;
    size_t expected_size = 0;
    while (request.size() < max_request_bytes) {
        const ssize_t n = recv(client_fd, buffer, sizeof(buffer), 0);
        if (n <= 0) return;
        request.append(buffer, static_cast<size_t>(n));
        if (header_end == std::string::npos) {
            header_end = request.find("\r\n\r\n");
            if (header_end != std::string::npos) {
                expected_size = header_end + 4 + content_length(request.substr(0, header_end + 4));
                if (expected_size > max_request_bytes) {
                    const auto response = http_response(413, R"({"error":"upload exceeds 100MB limit"})");
                    send(client_fd, response.c_str(), response.size(), MSG_NOSIGNAL);
                    return;
                }
            }
        }
        if (header_end != std::string::npos && request.size() >= expected_size) break;
    }
    if (header_end == std::string::npos || request.size() < expected_size) return;

    const std::string header = request.substr(0, header_end + 4);
    const std::string body = request.substr(header_end + 4);
    std::string method = parse_request_method(header);
    std::string path = parse_request_path(header);
    const std::string route = path.substr(0, path.find('?'));

    std::string response;

    if ((route == "/health" || route == "/api/health") && method == "GET") {
        response = handle_health();
    }
    else if ((route == "/status" || route == "/api/status") && method == "GET") {
        response = handle_status();
    }
    else if ((route == "/monitor" || route == "/api/monitor") && method == "GET") {
        response = handle_monitor();
    }
    else if ((route == "/models" || route == "/api/models") && method == "GET") {
        response = handle_list_models();
    }
    else if (route == "/api/logs" && method == "GET") {
        response = handle_logs(path);
    }
    else if (route == "/api/logs" && method == "DELETE") {
        response = handle_logs_clear();
    }
    else if (route == "/api/parameters" && method == "GET") {
        response = handle_parameters_get();
    }
    else if (route == "/api/parameters" && method == "PUT") {
        response = handle_parameters_set(body);
    }
    else if (route == "/api/parameters/reset" && method == "POST") {
        response = handle_parameters_reset();
    }
    else if (route == "/api/presets" && method == "GET") {
        response = handle_presets_get();
    }
    else if (route == "/api/presets" && method == "POST") {
        response = handle_presets_post(body);
    }
    else if (route.find("/api/presets/") == 0 && method == "DELETE") {
        response = handle_presets_delete(route);
    }
    else if (route == "/api/mode/switch" && method == "POST") {
        response = handle_mode_switch(body);
    }
    else if (route == "/api/file/convert" && method == "POST") {
        response = handle_file_upload(header, body);
    }
    else if (route == "/api/file/status" && method == "GET") {
        response = handle_file_status(path);
    }
    else if (route == "/api/file/result" && method == "GET") {
        response = handle_file_result(client_fd, path);
        if (response.empty()) return;
    }
    else if (route == "/api/subtitles" && method == "GET") {
        // SSE 长连接：dup 出独立 fd 交给受跟踪的工作线程 tail 字幕 JSONL，
        // 原连接立即返回由 run_server 关闭（dup 引用同一 TCP 连接）。
        const int sse_fd = dup(client_fd);
        if (sse_fd < 0) {
            const auto response = http_response(500, R"({"error":"failed to open subtitle stream"})");
            send(client_fd, response.c_str(), response.size(), MSG_NOSIGNAL);
            return;
        }
#ifdef _WIN32
        const DWORD send_timeout_ms = 5000;
        setsockopt(sse_fd, SOL_SOCKET, SO_SNDTIMEO,
                   reinterpret_cast<const char*>(&send_timeout_ms), sizeof(send_timeout_ms));
#else
        struct timeval send_timeout{5, 0};
        setsockopt(sse_fd, SOL_SOCKET, SO_SNDTIMEO, &send_timeout, sizeof(send_timeout));
#endif
        auto done = std::make_shared<std::atomic<bool>>(false);
        try {
            std::thread worker([this, sse_fd, done]() {
                handle_subtitles_stream(sse_fd);
                socket_close(sse_fd);
                done->store(true);
            });
            std::lock_guard<std::mutex> lock(sse_mutex_);
            reap_finished_sse_locked();
            sse_workers_.push_back({std::move(worker), done});
        } catch (const std::system_error& error) {
            spdlog::error("Failed to start subtitle SSE worker: {}", error.what());
            socket_close(sse_fd);
            const auto response = http_response(500, R"({"error":"failed to start subtitle stream"})");
            send(client_fd, response.c_str(), response.size(), MSG_NOSIGNAL);
        }
        return;
    }
    else if (route == "/api/file/cancel" && method == "DELETE") {
        response = handle_file_cancel(path);
    }
    else if (route == "/api/file/pause" && method == "POST") {
        response = handle_file_pause();
    }
    else if (route == "/api/file/resume" && method == "POST") {
        response = handle_file_resume();
    }
    else if (route == "/api/file/finished" && method == "DELETE") {
        response = handle_file_clear_finished();
    }
    else if (route == "/api/file/job" && method == "DELETE") {
        response = handle_file_remove(path);
    }
    else if ((route.find("/models/") == 0 || route.find("/api/models/") == 0)
             && route.find("/activate") != std::string::npos && method == "POST") {
        const size_t start = route.find("/api/models/") == 0 ? 12 : 8;
        size_t end = route.find("/activate", start);
        std::string model_id = route.substr(start, end - start);
        response = handle_activate_model(model_id);
    }
    else {
        response = http_response(404, R"({"error":"not found"})");
    }

    send(client_fd, response.c_str(), response.size(), MSG_NOSIGNAL);
}

std::string HttpApiServer::handle_health() {
    return http_response(200, R"({"status":"ok"})");
}

void HttpApiServer::handle_subtitles_stream(int client_fd) {
    static const char* kHead =
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/event-stream\r\n"
        "Cache-Control: no-cache\r\n"
        "Connection: keep-alive\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "\r\n";
    if (send(client_fd, kHead, std::strlen(kHead), MSG_NOSIGNAL) < 0) return;

    const char* env = std::getenv("MOZART_SUBTITLES_JSONL");
    const std::string file_path = env && env[0] ? env : "/tmp/opencode/subtitles.jsonl";

    std::ifstream file;
    auto try_open = [&]() {
        file.close();
        file.clear();
        file.open(file_path);
        if (file) file.seekg(0, std::ios::end); // 只推新增行
    };
    try_open();
    auto last_retry = std::chrono::steady_clock::now();

    while (running_) {
        if (!file.is_open()) {
            const auto now = std::chrono::steady_clock::now();
            if (now - last_retry > std::chrono::seconds(2)) {
                try_open();
                last_retry = now;
            }
        } else {
            std::string line;
            while (std::getline(file, line)) {
                if (line.empty()) continue;
                const std::string payload = "data: " + line + "\n\n";
                if (send(client_fd, payload.c_str(), payload.size(), MSG_NOSIGNAL) < 0) {
                    return; // 客户端断开
                }
            }
            file.clear(); // EOF：等待新行
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
}


std::string HttpApiServer::handle_status() {
    return http_response(200, controller_ ? controller_->status().dump(2) : R"({"error":"controller not initialized"})");
}

std::string HttpApiServer::handle_monitor() {
    auto snapshot = monitor_.snapshot();
    snapshot["vad"] = controller_ ? controller_->status().value("vad", nlohmann::json{{"available", false}}) : nlohmann::json{{"available", false}};
    return http_response(200, snapshot.dump());
}

std::string HttpApiServer::handle_logs(const std::string& path) {
    size_t limit = 200;
    try {
        const std::string requested = query_value(path, "limit");
        if (!requested.empty()) limit = std::stoull(requested);
    } catch (...) {}
    return http_response(200, controller_ ? controller_->logs(limit).dump() : R"({"error":"controller not initialized"})");
}

std::string HttpApiServer::handle_logs_clear() {
    clear_backend_logs();
    return http_response(200, R"({"status":"cleared"})");
}

std::string HttpApiServer::handle_parameters_get() {
    return http_response(200, controller_ ? controller_->parameters().dump() : R"({"error":"controller not initialized"})");
}

std::string HttpApiServer::handle_parameters_set(const std::string& body) {
    try {
        const auto result = controller_->set_parameters(nlohmann::json::parse(body));
        const std::string status = result.value("status", "");
        const int code = status == "busy" ? 409 : status == "invalid" ? 422 : 200;
        return http_response(code, result.dump());
    } catch (const std::exception&) {
        return http_response(400, R"({"error":"invalid parameter JSON"})");
    }
}

std::string HttpApiServer::handle_parameters_reset() {
    const auto result = controller_->reset_parameters();
    const int code = result.value("status", "") == "busy" ? 409 : result.value("status", "") == "invalid" ? 422 : 200;
    return http_response(code, result.dump());
}

std::string HttpApiServer::handle_presets_get() {
    return http_response(200, controller_->list_presets().dump());
}

std::string HttpApiServer::handle_presets_post(const std::string& body) {
    try {
        const auto result = controller_->save_preset(nlohmann::json::parse(body));
        const int code = result.value("status", "") == "invalid" ? 422 : result.value("status", "") == "failed" ? 500 : 200;
        return http_response(code, result.dump());
    } catch (const std::exception&) {
        return http_response(400, R"({"error":"invalid preset JSON"})");
    }
}

std::string HttpApiServer::handle_presets_delete(const std::string& path) {
    const std::string id = path.substr(std::string("/api/presets/").size());
    const auto result = controller_->delete_preset(id);
    return http_response(result.value("status", "") == "deleted" ? 200 : 404, result.dump());
}

std::string HttpApiServer::handle_list_models() {
    return http_response(200, controller_ ? controller_->list_models().dump(2) : R"({"error":"controller not initialized"})");
}

std::string HttpApiServer::handle_mode_switch(const std::string& body) {
    try {
        const auto request = nlohmann::json::parse(body);
        const std::string mode = request.value("mode", "");
        const std::string model_id = request.value("model_id", request.value("speaker_id", ""));
        if (!model_id.empty() && !valid_model_id(model_id)) {
            return http_response(422, R"({"status":"invalid","error":"invalid model_id"})");
        }
        const auto result = controller_->request_mode(mode, model_id);
        const std::string status = result.value("status", "");
        const int code = status == "unavailable" ? 501
            : status == "invalid" ? 422
            : (status == "failed" || status == "busy") ? 409
            : status == "switching_deferred" ? 202 : 200;
        return http_response(code, result.dump());
    } catch (const std::exception&) {
        return http_response(400, R"({"error":"invalid JSON request"})");
    }
}

std::string HttpApiServer::handle_file_upload(const std::string& header, const std::string& body) {
    const auto reject = [](int code, const char* message) {
        spdlog::warn("[file_rvc] upload rejected: {}", message);
        return http_response(code, std::string(R"({"error":")") + message + R"("})");
    };
    const auto boundary_position = header.find("boundary=");
    if (boundary_position == std::string::npos) return reject(400, "multipart boundary is required");
    std::string boundary = header.substr(boundary_position + 9);
    const auto boundary_end = boundary.find_first_of("\r\n;");
    if (boundary_end != std::string::npos) boundary.erase(boundary_end);
    const std::string filename_marker = "filename=\"";
    const auto filename_start = body.find(filename_marker);
    if (filename_start == std::string::npos) return reject(400, "audio_file is required");
    const auto filename_end = body.find('"', filename_start + filename_marker.size());
    if (filename_end == std::string::npos) return reject(400, "invalid audio_file filename");
    const std::string filename = body.substr(filename_start + filename_marker.size(), filename_end - filename_start - filename_marker.size());
    const std::string data_start_marker = "\r\n\r\n";
    const auto data_start = body.find(data_start_marker, filename_end);
    const auto data_end = body.find("\r\n--" + boundary, data_start + data_start_marker.size());
    if (data_start == std::string::npos || data_end == std::string::npos) return reject(400, "invalid audio_file payload");
    const auto destination = controller_->upload_path(filename);
    std::ofstream uploaded(destination, std::ios::binary | std::ios::trunc);
    uploaded.write(body.data() + data_start + data_start_marker.size(), static_cast<std::streamsize>(data_end - data_start - data_start_marker.size()));
    uploaded.flush();
    if (!uploaded) {
        uploaded.close();
        std::error_code error;
        std::filesystem::remove(destination, error);
        return reject(500, "failed to store upload");
    }
    uploaded.close();
    if (!uploaded) {
        std::error_code error;
        std::filesystem::remove(destination, error);
        return reject(500, "failed to store upload");
    }
    std::string model_id = multipart_value(body, boundary, "model_id");
    if (model_id.empty()) model_id = multipart_value(body, boundary, "speaker_id");
    if (!model_id.empty() && !valid_model_id(model_id)) {
        std::error_code error;
        std::filesystem::remove(destination, error);
        return reject(422, "invalid model_id");
    }
    const auto result = controller_->enqueue_file(destination, filename, model_id);
    if (result.value("status", "") == "rejected") {
        std::error_code error;
        std::filesystem::remove(destination, error);
        spdlog::warn("[file_rvc] upload rejected for {}: {}", filename, result.value("error", "unknown error"));
        return http_response(409, result.dump());
    }
    spdlog::info("[file_rvc] upload queued: {}", filename);
    return http_response(200, result.dump());
}

std::string HttpApiServer::handle_file_status(const std::string& path) {
    const auto result = controller_->job_status(query_value(path, "job_id"));
    return http_response(result.contains("job_id") ? 200 : 404, result.dump());
}

std::string HttpApiServer::handle_file_cancel(const std::string& path) {
    const auto result = controller_->cancel_job(query_value(path, "job_id"));
    return http_response(result.contains("job_id") ? 200 : 404, result.dump());
}

std::string HttpApiServer::handle_file_pause() {
    const auto result = controller_->pause_file_queue();
    return http_response(result.value("status", "") == "paused" ? 200 : 409, result.dump());
}

std::string HttpApiServer::handle_file_resume() {
    const auto result = controller_->resume_file_queue();
    return http_response(result.value("status", "") == "active" ? 200 : 409, result.dump());
}

std::string HttpApiServer::handle_file_remove(const std::string& path) {
    const auto result = controller_->remove_job(query_value(path, "job_id"));
    return http_response(result.value("status", "") == "removed" ? 200 : 409, result.dump());
}

std::string HttpApiServer::handle_file_clear_finished() {
    return http_response(200, controller_->clear_finished_jobs().dump());
}

std::string HttpApiServer::handle_file_result(int client_fd, const std::string& path) {
    const auto output = controller_->completed_output(query_value(path, "job_id"));
    if (!output) return http_response(404, R"({"error":"completed output not found"})");
    std::ifstream stream(*output, std::ios::binary);
    std::error_code error;
    const auto size = std::filesystem::file_size(*output, error);
    if (!stream || error) return http_response(404, R"({"error":"completed output was removed"})");
    const std::string header = "HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\nContent-Length: " + std::to_string(size)
        + "\r\nContent-Disposition: attachment; filename=\"mozart_output.wav\"\r\nConnection: close\r\n\r\n";
    if (send(client_fd, header.c_str(), header.size(), MSG_NOSIGNAL) < 0) return "";
    char buffer[8192];
    while (stream.read(buffer, sizeof(buffer)) || stream.gcount() > 0) {
        if (send(client_fd, buffer, static_cast<size_t>(stream.gcount()), MSG_NOSIGNAL) < 0) return "";
    }
    return "";
}

std::string HttpApiServer::handle_activate_model(const std::string& model_id) {
    spdlog::info("Activate model request: {}", model_id);
    if (!valid_model_id(model_id)) {
        return http_response(422, R"({"status":"invalid","error":"invalid model_id"})");
    }

    const auto result = controller_->request_mode(controller_->status().value("mode", "idle"), model_id);
    const std::string status = result.value("status", "");
    const int code = status == "failed" ? 404
        : (status == "busy" ? 409
        : (status == "invalid" ? 422
        : (status == "switching_deferred" ? 202 : 200)));
    return http_response(code, result.dump());
}

} // namespace rvc
