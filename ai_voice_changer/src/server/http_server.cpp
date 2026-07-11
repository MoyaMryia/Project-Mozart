// server/http_server.cpp
#include "server/http_server.hpp"
#include "utils/logging.hpp"

#ifdef MOZART_ENABLE_HTTP
#include <httplib.h>
#endif

#include <chrono>
#include <sstream>
#include <thread>

namespace mozart {

#ifdef MOZART_ENABLE_HTTP

namespace {

std::string models_to_json(const std::vector<RVCModelInfo>& models,
                           const std::string& current_id) {
    std::ostringstream os;
    os << "{\"models\":[";
    for (size_t i = 0; i < models.size(); ++i) {
        if (i) os << ",";
        os << "{\"id\":\"" << models[i].id
           << "\",\"exists\":" << (models[i].loaded ? "true" : "false")
           << ",\"current\":" << (models[i].id == current_id ? "true" : "false")
           << "}";
    }
    os << "]}";
    return os.str();
}

}  // anonymous namespace

struct HttpServer::Impl {
    httplib::Server   svr;
    std::thread       worker;
    std::atomic<bool> bound{false};
};

HttpServer::HttpServer(const std::string& host,
                       int port,
                       ModelManager& models,
                       PipelineStats& stats,
                       const std::string& mode_name)
    : impl_(std::make_unique<Impl>()) {
    auto& svr = impl_->svr;

    svr.Get("/health", [](const httplib::Request&, httplib::Response& res) {
        res.set_content("{\"status\":\"ok\"}", "application/json");
    });

    svr.Get("/status",
            [&](const httplib::Request&, httplib::Response& res) {
        std::ostringstream os;
        os << "{\"mode\":\"" << mode_name << "\","
           << "\"frames_processed\":" << stats.frames_processed.load() << ","
           << "\"frames_silence\":" << stats.frames_silence.load() << ","
           << "\"last_latency_ms\":" << stats.last_latency_ms.load() << ","
           << "\"avg_latency_ms\":" << stats.avg_latency_ms.load() << ","
           << "\"current_model\":\"" << models.current_model_id() << "\"}";
        res.set_content(os.str(), "application/json");
    });

    svr.Get("/models",
            [&](const httplib::Request&, httplib::Response& res) {
        auto m = models.list_models();
        res.set_content(models_to_json(m, models.current_model_id()),
                        "application/json");
    });

    svr.Post("/models/upload",
             [&](const httplib::Request& req, httplib::Response& res) {
        auto mv = req.get_file_value("model_id");
        std::string id = mv.content;
        if (id.find('/') != std::string::npos ||
            id.find('\\') != std::string::npos || id.empty()) {
            res.status = 400;
            res.set_content("invalid model_id", "text/plain");
            return;
        }
        // Skeleton: store files to models/{id}/. Full impl reads each
        // part via req.get_file_value and writes to disk. Mark received
        // status now; expand once the multipart flow is finalized.
        MOZART_INFO("Model upload received for id='%s' (skeleton)", id.c_str());
        res.set_content("{\"status\":\"received\",\"model_id\":\"" + id + "\"}",
                        "application/json");
    });

    svr.Post(R"(/models/(.+)/activate)",
             [&](const httplib::Request& req, httplib::Response& res) {
        std::string id = req.matches[1];
        if (!models.activate(id)) {
            res.status = 400;
            res.set_content("activate failed", "text/plain");
            return;
        }
        res.set_content("{\"status\":\"activated\",\"model_id\":\"" + id + "\"}",
                        "application/json");
    });

    impl_->worker = std::thread([this, host, port]() {
        if (!impl_->svr.listen(host.c_str(), port)) {
            MOZART_ERROR("HTTP server failed to bind %s:%d",
                         host.c_str(), port);
        }
    });

    MOZART_INFO("HTTP server listening on %s:%d", host.c_str(), port);
}

HttpServer::~HttpServer() {
    if (impl_) {
        if (impl_->svr.is_running()) impl_->svr.stop();
        if (impl_->worker.joinable())  impl_->worker.join();
    }
}

void HttpServer::wait_ready() {
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
}

#else  // MOZART_ENABLE_HTTP

struct HttpServer::Impl {};

HttpServer::HttpServer(const std::string& host,
                       int port,
                       ModelManager& models,
                       PipelineStats& stats,
                       const std::string& mode_name)
    : impl_(std::make_unique<Impl>()) {
    (void)host; (void)port; (void)models; (void)stats; (void)mode_name;
    MOZART_WARN("HTTP server disabled (build without -DMOZART_ENABLE_HTTP=ON)");
}

HttpServer::~HttpServer() = default;
void HttpServer::wait_ready() {}

#endif  // MOZART_ENABLE_HTTP

}  // namespace mozart