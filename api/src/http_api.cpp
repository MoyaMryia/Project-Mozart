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
#include <sys/stat.h>
#include <poll.h>
#include <cerrno>
#include <cmath>
#include <array>
#include <fcntl.h>
#include <sys/inotify.h>

// 写已断开的 socket 不应让进程收到 SIGPIPE（Linux）。没有该标志时退化为 0。
#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif

namespace rvc {

static std::string http_response(int code, const std::string& body, const std::string& content_type = "application/json") {
    std::string status;
    switch (code) {
        case 200: status = "200 OK"; break;
        case 202: status = "202 Accepted"; break;
        case 408: status = "408 Request Timeout"; break;
        case 431: status = "431 Request Header Fields Too Large"; break;
        case 503: status = "503 Service Unavailable"; break;
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

using Clock = std::chrono::steady_clock;

static bool wait_socket(int fd, short events, Clock::time_point deadline) {
    while (Clock::now() < deadline) {
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - Clock::now());
        pollfd descriptor{fd, events, 0};
        const int result = poll(&descriptor, 1, static_cast<int>(std::max<int64_t>(1, remaining.count())));
        if (result > 0) return (descriptor.revents & events) != 0;
        if (result < 0 && errno != EINTR) return false;
    }
    return false;
}

static bool send_all(int fd, const char* data, size_t size, Clock::time_point deadline) {
    size_t sent = 0;
    while (sent < size && wait_socket(fd, POLLOUT, deadline)) {
        const auto count = send(fd, data + sent, size - sent, MSG_NOSIGNAL | MSG_DONTWAIT);
        if (count > 0) sent += static_cast<size_t>(count);
        else if (count == 0 || (errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK)) return false;
    }
    return sent == size;
}

static bool send_all(int fd, const std::string& data, Clock::time_point deadline) {
    return send_all(fd, data.data(), data.size(), deadline);
}

// The reference-TTS worker owns its engine and queue in a separate process.
// Keep inference off this control server; proxy only bounded metadata and WAVs.
static std::string speech_request(const std::string& method, const std::string& path,
                                  const std::string& body) {
    const auto unavailable = []() {
        return std::string("HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n")
            + R"({"error":"Reference speech service unavailable; start tools/speech_service.py"})";
    };
    const char* env = std::getenv("MOZART_SPEECH_PORT");
    int port = 18081;
    try { if (env && env[0]) port = std::stoi(env); } catch (...) { return unavailable(); }
    if (port < 1 || port > 65535) return unavailable();
    if (body.size() > 7ULL * 1024 * 1024)
        return http_response(413, R"({"error":"Speech request exceeds 7 MiB"})");
    const int fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0) return unavailable();
    const auto deadline = Clock::now() + std::chrono::seconds(5);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(static_cast<uint16_t>(port));
    inet_pton(AF_INET, "127.0.0.1", &address.sin_addr);
    if (connect(fd, reinterpret_cast<sockaddr*>(&address), sizeof(address)) != 0) {
        if (errno != EINPROGRESS || !wait_socket(fd, POLLOUT, deadline)) {
            socket_close(fd);
            return unavailable();
        }
        int error = 0;
        socklen_t size = sizeof(error);
        if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &size) != 0 || error != 0) {
            socket_close(fd);
            return unavailable();
        }
    }
    const std::string request = method + " " + path + " HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        "Content-Type: application/json\r\nConnection: close\r\nContent-Length: "
        + std::to_string(body.size()) + "\r\n\r\n" + body;
    if (!send_all(fd, request, deadline)) { socket_close(fd); return unavailable(); }
    std::string response;
    char buffer[8192];
    while (wait_socket(fd, POLLIN, deadline)) {
        const auto count = recv(fd, buffer, sizeof(buffer), MSG_DONTWAIT);
        if (count == 0) {
            socket_close(fd);
            return response.empty() ? unavailable() : response;
        }
        if (count < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
            break;
        }
        response.append(buffer, static_cast<size_t>(count));
        if (response.size() > 10ULL * 1024 * 1024) break;
    }
    socket_close(fd);
    return unavailable();
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

static bool content_length(const std::string& header, size_t& value) {
    bool found = false;
    value = 0;
    auto position = header.find("\r\n") + 2;
    while (position < header.size() - 2) {
        const auto end = header.find("\r\n", position);
        const auto colon = header.find(':', position);
        if (colon == std::string::npos || colon >= end) return false;
        auto name = header.substr(position, colon - position);
        std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return std::tolower(c); });
        if (name == "transfer-encoding") return false;
        if (name == "content-length") {
            if (found) return false;
            found = true;
            size_t start = colon + 1, stop = end;
            while (start < stop && (header[start] == ' ' || header[start] == '\t')) ++start;
            while (stop > start && (header[stop - 1] == ' ' || header[stop - 1] == '\t')) --stop;
            if (start == stop) return false;
            for (; start < stop; ++start) {
                const auto c = static_cast<unsigned char>(header[start]);
                if (c < '0' || c > '9' || value > (std::numeric_limits<size_t>::max() - (c - '0')) / 10) return false;
                value = value * 10 + (c - '0');
            }
        }
        position = end + 2;
    }
    return true;
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
    : HttpApiServer(host, port, controller, Limits{}) {}

HttpApiServer::HttpApiServer(const std::string& host, uint16_t port, ControlPlane* controller, Limits limits)
    : host_(host), port_(port), controller_(controller), limits_(limits) {
    const char* status_path = std::getenv("MOZART_STACK_STATUS_JSON");
    if (status_path) stack_status_path_ = status_path;
    const char* glossary_path = std::getenv("MOZART_GLOSSARY");
    if (glossary_path) glossary_path_ = glossary_path;
    const char* instance_id = std::getenv("MOZART_STACK_INSTANCE_ID");
    if (instance_id) stack_instance_id_ = instance_id;
}

HttpApiServer::~HttpApiServer() {
    stop();
}

bool HttpApiServer::start() {
    if (running_.load()) return false;

    workers_.reserve(limits_.connections);
    server_fd_ = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
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
        server_fd_ = -1;
        return false;
    }

    if (listen(server_fd_, 10) < 0) {
        spdlog::error("Failed to listen on HTTP socket");
        socket_close(server_fd_);
        server_fd_ = -1;
        return false;
    }

    running_ = true;
    server_thread_ = std::thread(&HttpApiServer::run_server, this);

    spdlog::info("HTTP API server listening on {}:{}", host_, port_);
    return true;
}

void HttpApiServer::stop() {
    running_ = false;
    if (server_fd_ >= 0) shutdown(server_fd_, SHUT_RDWR);
    {
        std::lock_guard<std::mutex> lock(workers_mutex_);
        for (const int fd : clients_) shutdown(fd, SHUT_RDWR);
    }
    if (server_thread_.joinable()) server_thread_.join();
    if (server_fd_ >= 0) {
        socket_close(server_fd_);
        server_fd_ = -1;
    }
    std::vector<Worker> workers;
    {
        std::lock_guard<std::mutex> lock(workers_mutex_);
        workers.swap(workers_);
    }
    for (auto& worker : workers) {
        if (worker.thread.joinable()) worker.thread.join();
    }
}

void HttpApiServer::reap_finished_workers_locked() {
    auto worker = workers_.begin();
    while (worker != workers_.end()) {
        if (worker->done->load()) {
            worker->thread.join();
            worker = workers_.erase(worker);
        } else ++worker;
    }
}

void HttpApiServer::run_server() {
    while (running_) {
        const int client_fd = accept4(server_fd_, nullptr, nullptr, SOCK_CLOEXEC);
        if (client_fd < 0) {
            if (!running_) break;
            continue;
        }
        std::lock_guard<std::mutex> lock(workers_mutex_);
        reap_finished_workers_locked();
        if (!running_ || workers_.size() >= limits_.connections) {
            const auto response = http_response(503, R"({"error":"connection limit reached"})");
            send(client_fd, response.data(), response.size(), MSG_NOSIGNAL | MSG_DONTWAIT);
            socket_close(client_fd);
            continue;
        }
        auto done = std::make_shared<std::atomic<bool>>(false);
        clients_.insert(client_fd);
        try {
            workers_.push_back({std::thread([this, client_fd, done]() {
                try {
                    handle_request(client_fd);
                } catch (const std::exception& error) {
                    spdlog::error("HTTP request failed: {}", error.what());
                    send_all(client_fd, http_response(500, R"({"error":"internal server error"})"),
                             Clock::now() + limits_.write_timeout);
                }
                std::lock_guard<std::mutex> lock(workers_mutex_);
                clients_.erase(client_fd);
                socket_close(client_fd);
                done->store(true);
            }), done});
        } catch (const std::system_error& error) {
            clients_.erase(client_fd);
            socket_close(client_fd);
            spdlog::error("Failed to start HTTP worker: {}", error.what());
        }
    }
}

void HttpApiServer::handle_request(int client_fd) {
    constexpr size_t max_request_bytes = 110ULL * 1024 * 1024;
    constexpr size_t max_header_bytes = 16 * 1024;
    struct RequestBudget {
        std::atomic<size_t>& total;
        size_t bytes = 0;
        ~RequestBudget() { total.fetch_sub(bytes); }
    } budget{request_bytes_};
    const auto start = Clock::now();
    const auto deadline = start + limits_.request_timeout;
    const auto reject = [&](int code, const char* error) {
        send_all(client_fd, http_response(code, nlohmann::json{{"error", error}}.dump()),
                 Clock::now() + limits_.write_timeout);
    };
    std::string request;
    char buffer[8192];
    size_t header_end = std::string::npos;
    size_t expected_size = 0;
    while (true) {
        const auto read_deadline = header_end == std::string::npos
            ? std::min(deadline, start + limits_.header_timeout) : deadline;
        if (!wait_socket(client_fd, POLLIN, read_deadline)) {
            if (running_) reject(408, "request deadline exceeded");
            return;
        }
        const ssize_t n = recv(client_fd, buffer, sizeof(buffer), MSG_DONTWAIT);
        if (n < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) continue;
        if (n <= 0) return;
        const size_t bytes = static_cast<size_t>(n);
        const size_t previous = request_bytes_.fetch_add(bytes);
        budget.bytes += bytes;
        if (previous + bytes > limits_.request_memory_bytes) {
            reject(503, "request memory limit reached");
            return;
        }
        request.append(buffer, static_cast<size_t>(n));
        if (header_end == std::string::npos) {
            header_end = request.find("\r\n\r\n");
            if ((header_end == std::string::npos && request.size() > max_header_bytes)
                || (header_end != std::string::npos && header_end + 4 > max_header_bytes)) {
                reject(431, "request header exceeds 16 KiB");
                return;
            }
            if (header_end != std::string::npos) {
                size_t length = 0;
                if (!content_length(request.substr(0, header_end + 4), length)) {
                    reject(400, "invalid request framing");
                    return;
                }
                if (length > max_request_bytes - header_end - 4) {
                    reject(413, "request exceeds 110 MiB");
                    return;
                }
                expected_size = header_end + 4 + length;
            }
        }
        if (header_end != std::string::npos && request.size() >= expected_size) break;
    }
    const std::string header = request.substr(0, header_end + 4);
    const std::string body = request.substr(header_end + 4, expected_size - header_end - 4);
    std::string method = parse_request_method(header);
    std::string path = parse_request_path(header);
    const std::string route = path.substr(0, path.find('?'));

    std::string response;

    if (route == "/api/voices" || route.find("/api/voices/") == 0
        || route.find("/api/speech/") == 0) {
        response = speech_request(method, path, body);
    }
    else if ((route == "/health" || route == "/api/health") && method == "GET") {
        response = handle_health();
    }
    else if (route == "/api/stack/status" && method == "GET") {
        response = handle_stack_status();
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
    else if (route == "/api/glossary" && method == "GET") {
        response = handle_glossary_get();
    }
    else if (route == "/api/glossary" && method == "PUT") {
        response = handle_glossary_set(body);
    }
    else if (route == "/api/parameters" && method == "GET") {
        response = handle_parameters_get();
    }
    else if (route == "/api/parameters" && method == "PUT") {
        response = handle_parameters_set(body);
    }
    else if (route == "/api/preprocessor" && method == "GET") {
        response = handle_preprocessor_get();
    }
    else if (route == "/api/preprocessor" && method == "PUT") {
        response = handle_preprocessor_set(body);
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
    else if (route == "/api/realtime/routing" && method == "POST") {
        response = handle_realtime_routing(body);
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
        if (subtitle_connections_.fetch_add(1) >= limits_.subtitle_connections) {
            subtitle_connections_.fetch_sub(1);
            response = http_response(503, R"({"error":"subtitle connection limit reached"})");
        } else {
            try {
                handle_subtitles_stream(client_fd);
            } catch (...) {
                subtitle_connections_.fetch_sub(1);
                throw;
            }
            subtitle_connections_.fetch_sub(1);
            return;
        }
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

    send_all(client_fd, response, Clock::now() + limits_.write_timeout);
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
    const char* env = std::getenv("MOZART_SUBTITLES_JSONL");
    const std::string file_path = env && env[0] ? env : "/tmp/opencode/subtitles.jsonl";

    struct Changes {
        int fd = inotify_init1(IN_NONBLOCK | IN_CLOEXEC);
        int watch = -1;
        ~Changes() { if (fd >= 0) close(fd); }
        bool arm(const std::string& parent) {
            if (fd < 0) return false;
            const int next = inotify_add_watch(fd, parent.c_str(),
                IN_MODIFY | IN_CLOSE_WRITE | IN_CREATE | IN_MOVED_TO | IN_DELETE
                | IN_MOVE_SELF | IN_DELETE_SELF);
            if (next < 0) return false;
            if (watch >= 0 && next != watch) inotify_rm_watch(fd, watch);
            watch = next;
            return true;
        }
    } changes;
    const auto parent_path = std::filesystem::path(file_path).parent_path();
    const std::string parent = parent_path.empty() ? "." : parent_path.string();
    changes.arm(parent);

    std::ifstream file;
    struct stat identity{};
    bool opened_once = false;
    auto try_open = [&]() {
        file.close();
        file.clear();
        file.open(file_path);
        if (file) {
            stat(file_path.c_str(), &identity);
            if (!opened_once) file.seekg(0, std::ios::end);
            opened_once = true; // Replacement logs start at byte zero.
        }
    };
    try_open();
    opened_once = true;
    // 在响应头发出前确定起点，避免客户端随后追加的首条字幕被跳过。
    if (!send_all(client_fd, kHead, std::strlen(kHead), Clock::now() + limits_.write_timeout)) return;
    auto last_heartbeat = Clock::now();

    while (running_) {
        if (Clock::now() - last_heartbeat >= std::chrono::seconds(1)) {
            if (!send_all(client_fd, ": heartbeat\n\n", Clock::now() + limits_.write_timeout)) return;
            last_heartbeat = Clock::now();
        }
        struct stat current{};
        if (file.is_open() && stat(file_path.c_str(), &current) == 0) {
            const auto position = file.tellg();
            const bool rewritten_at_end = position >= 0 && current.st_size == position
                && current.st_size == identity.st_size
                && (current.st_mtim.tv_sec != identity.st_mtim.tv_sec
                    || current.st_mtim.tv_nsec != identity.st_mtim.tv_nsec);
            if (current.st_ino != identity.st_ino || current.st_dev != identity.st_dev
                || (position >= 0 && current.st_size < position) || rewritten_at_end) {
                try_open();
            } else {
                identity = current;
            }
        }
        if (!file.is_open()) {
            try_open();
        } else {
            std::string line;
            while (file && running_) {
                const auto position = file.tellg();
                if (!std::getline(file, line)) break;
                if (file.eof()) { file.clear(); file.seekg(position); break; }
                if (line.empty()) continue;
                const std::string payload = "data: " + line + "\n\n";
                if (!send_all(client_fd, payload, Clock::now() + limits_.write_timeout)) {
                    return; // 客户端断开
                }
            }
            file.clear(); // EOF：等待新行
        }
        const bool watching = changes.arm(parent);
        pollfd waiters[2] = {{client_fd, POLLIN, 0}, {changes.fd, POLLIN, 0}};
        const int ready = poll(waiters, changes.fd >= 0 ? 2 : 1, watching ? 1000 : 20);
        if (ready < 0 && errno != EINTR) return;
        if (waiters[0].revents & (POLLIN | POLLHUP | POLLERR | POLLNVAL)) return;
        if (changes.fd >= 0 && waiters[1].revents & POLLIN) {
            char events[4096];
            while (read(changes.fd, events, sizeof(events)) > 0) {}
        }
    }
}


std::string HttpApiServer::handle_status() {
    return http_response(200, controller_ ? controller_->status().dump(2) : R"({"error":"controller not initialized"})");
}

// 固定路径只读记录。实例和父子关系用于识别残留记录，不用于控制进程。
std::string HttpApiServer::handle_stack_status() {
    using Json = nlohmann::json;
    const auto unavailable = [](const std::string& error) {
        return http_response(503, Json{{"managed", true}, {"available", false}, {"error", error}}.dump());
    };
    if (stack_status_path_.empty())
        return http_response(200, Json{{"managed", false}, {"available", false}}.dump());
    if (stack_instance_id_.empty()) return unavailable("Supervisor instance is not configured");
    struct RecordFile {
        int fd;
        ~RecordFile() { if (fd >= 0) close(fd); }
    } file{open(stack_status_path_.c_str(), O_RDONLY | O_NONBLOCK | O_CLOEXEC)};
    if (file.fd < 0) return unavailable(std::string("Supervisor status open failed: ") + std::strerror(errno));
    struct stat metadata{};
    if (fstat(file.fd, &metadata) < 0)
        return unavailable(std::string("Supervisor status metadata failed: ") + std::strerror(errno));
    if (!S_ISREG(metadata.st_mode)) return unavailable("Supervisor status must be a regular file");
    constexpr size_t kMaxStackStatusBytes = 64 * 1024;
    if (metadata.st_size > static_cast<off_t>(kMaxStackStatusBytes))
        return unavailable("Supervisor status exceeds " + std::to_string(kMaxStackStatusBytes) + " bytes");
    std::array<char, kMaxStackStatusBytes + 1> buffer{};
    size_t used = 0;
    while (used < buffer.size()) {
        const auto count = read(file.fd, buffer.data() + used, buffer.size() - used);
        if (count < 0) {
            if (errno == EINTR) continue;
            return unavailable(std::string("Supervisor status read failed: ") + std::strerror(errno));
        }
        if (count == 0) break;
        used += static_cast<size_t>(count);
    }
    if (used > kMaxStackStatusBytes)
        return unavailable("Supervisor status exceeds " + std::to_string(kMaxStackStatusBytes) + " bytes");
    Json record;
    try {
        record = Json::parse(buffer.data(), buffer.data() + used);
    } catch (const Json::parse_error&) {
        return unavailable("Supervisor status contains invalid JSON");
    } catch (const Json::out_of_range&) {
        return unavailable("Supervisor status numeric value is out of range");
    }
    const auto text = [](const Json& value, const char* key) {
        return value.contains(key) && value[key].is_string() && !value[key].get_ref<const std::string&>().empty();
    };
    const auto boolean = [](const Json& value, const char* key) {
        return value.contains(key) && value[key].is_boolean();
    };
    if (!record.is_object() || !record.contains("version") || !record["version"].is_number_integer() || record["version"] != 1
        || !text(record, "state") || !text(record, "phase") || !text(record, "mode")
        || !record.contains("updated_at") || !record["updated_at"].is_number()
        || !std::isfinite(record["updated_at"].get<double>()) || record["updated_at"].get<double>() <= 0
        || !record.contains("readiness") || !record["readiness"].is_object()
        || !boolean(record["readiness"], "translation") || !boolean(record["readiness"], "speech_runtime")
        || !boolean(record["readiness"], "voice_session")
        || !record.contains("requirements") || !record["requirements"].is_object()
        || !text(record["requirements"], "language") || !boolean(record["requirements"], "playback")
        || !boolean(record["requirements"], "voice_required")
        || !record.contains("degraded") || !record["degraded"].is_object())
        return unavailable("Supervisor status schema is invalid");
    for (const auto& reason : record["degraded"])
        if (!reason.is_string()) return unavailable("Supervisor degraded reasons must be strings");
    if (record.contains("error") && (!record["error"].is_object() || !text(record["error"], "message")))
        return unavailable("Supervisor error record is invalid");
    const pid_t parent = getppid();
    if (parent <= 1 || record.value("instance_id", Json{}) != stack_instance_id_
        || !record.value("supervisor_pid", Json{}).is_number_integer()
        || record.value("supervisor_pid", Json{}) != parent
        || !record.contains("children") || !record["children"].is_object()
        || !record["children"].contains("backend") || !record["children"]["backend"].is_object()
        || !record["children"]["backend"].value("pid", Json{}).is_number_integer()
        || record["children"]["backend"].value("pid", Json{}) != getpid())
        return unavailable("Supervisor status does not match the active instance");
    record["managed"] = true;
    record["available"] = true;
    return http_response(200, record.dump());
}

std::string HttpApiServer::handle_monitor() {
    std::lock_guard<std::mutex> lock(monitor_mutex_);
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

namespace {
// 词条键为中文，释义为英文。校验放在写入前，加载时的错误不会污染管线。
bool valid_entry(const std::string& term, const std::string& meaning) {
    if (term.empty() || meaning.empty()) return false;
    bool chinese = false;
    for (unsigned char c : term) {
        if ((c & 0x80) != 0) chinese = true;
        else if (!std::isspace(c)) return false;
    }
    if (!chinese) return false;
    for (unsigned char c : meaning) {
        if (c >= 0x80) return false;
        if (!std::isalpha(c) && !std::isspace(c) && c != '-' && c != '\'') return false;
    }
    return std::isalpha(static_cast<unsigned char>(meaning.front())) != 0;
}
}  // namespace

std::string HttpApiServer::handle_glossary_get() {
    if (glossary_path_.empty())
        return http_response(409, R"({"error":"未配置 MOZART_GLOSSARY"})");
    std::ifstream file(glossary_path_);
    if (!file) return http_response(200, R"({"terms":{}})");
    nlohmann::json terms = nlohmann::json::parse(file, nullptr, false);
    if (terms.is_discarded() || !terms.is_object())
        return http_response(500, R"({"error":"术语表文件内容不是对象"})");
    return http_response(200, nlohmann::json{{"terms", terms}, {"path", glossary_path_}}.dump());
}

std::string HttpApiServer::handle_glossary_set(const std::string& body) {
    if (glossary_path_.empty())
        return http_response(409, R"({"error":"未配置 MOZART_GLOSSARY"})");
    nlohmann::json request = nlohmann::json::parse(body, nullptr, false);
    if (request.is_discarded() || !request.is_object())
        return http_response(400, R"({"error":"请求体必须是对象"})");
    const nlohmann::json terms = request.contains("terms") ? request["terms"] : request;
    if (!terms.is_object())
        return http_response(400, R"({"error":"terms 必须是对象"})");
    for (const auto& item : terms.items()) {
        if (!item.value().is_string() || !valid_entry(item.key(), item.value().get<std::string>()))
            return http_response(400, R"({"error":"词条键必须是中文，释义必须是英文"})");
    }
    const std::string temporary = glossary_path_ + ".tmp";
    {
        std::ofstream out(temporary, std::ios::trunc);
        if (!out) return http_response(500, R"({"error":"无法写入术语表"})");
        out << terms.dump(2) << '\n';
        if (!out.good()) return http_response(500, R"({"error":"写入术语表失败"})");
    }
    if (std::rename(temporary.c_str(), glossary_path_.c_str()) != 0)
        return http_response(500, R"({"error":"替换术语表失败"})");
    return http_response(200, nlohmann::json{{"terms", terms}, {"path", glossary_path_}}.dump());
}

std::string HttpApiServer::handle_parameters_get() {
    return http_response(200, controller_ ? controller_->parameters().dump() : R"({"error":"controller not initialized"})");
}

std::string HttpApiServer::handle_preprocessor_get() {
    if (!controller_) return http_response(503, R"({"error":"controller not initialized"})");
    return http_response(200, controller_->preprocessor_status().dump());
}

std::string HttpApiServer::handle_preprocessor_set(const std::string& body) {
    if (!controller_) return http_response(503, R"({"error":"controller not initialized"})");
    nlohmann::json request;
    try {
        request = nlohmann::json::parse(body);
    } catch (const std::exception&) {
        return http_response(400, R"({"error":"invalid preprocessor JSON"})");
    }
    const auto result = controller_->set_preprocessor(request);
    const std::string status = result.value("status", "");
    const int code = status == "invalid" ? 422 : status == "failed" ? 502 : 200;
    return http_response(code, result.dump());
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

std::string HttpApiServer::handle_realtime_routing(const std::string& body) {
    try {
        const auto request = nlohmann::json::parse(body);
        const auto result = controller_->set_realtime_routing(request);
        const int code = result.value("status", "") == "ok" ? 200 : 409;
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
    if (data_end - data_start - data_start_marker.size() > 100ULL * 1024 * 1024)
        return reject(413, "audio file exceeds 100 MiB");
    const auto destination = controller_->upload_path(filename);
    struct UploadCleanup {
        std::filesystem::path path;
        ~UploadCleanup() {
            std::error_code error;
            std::filesystem::remove(path, error);
        }
    } cleanup{destination};
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
    const auto deadline = Clock::now() + limits_.write_timeout;
    if (!send_all(client_fd, header, deadline)) return "";
    char buffer[8192];
    while (stream.read(buffer, sizeof(buffer)) || stream.gcount() > 0) {
        if (!send_all(client_fd, buffer, static_cast<size_t>(stream.gcount()), deadline)) return "";
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
