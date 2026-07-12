#include <mozart/server/http_server.hpp>
#include <mozart/utils/logging.hpp>

// cpp-httplib is header-only, vendored in third_party/
// For now, use a simple stub implementation
// TODO: Integrate cpp-httplib when vendored

namespace mozart {

struct HttpServer::Impl {
    Config cfg;
    Callbacks cbs;
    bool running = false;
};

HttpServer::HttpServer(const Config& cfg, const Callbacks& cbs)
    : impl_(std::make_unique<Impl>()) {
    impl_->cfg = cfg;
    impl_->cbs = cbs;
}

HttpServer::~HttpServer() {
    stop();
}

void HttpServer::start() {
    if (impl_->running) return;
    impl_->running = true;

    // TODO: Implement actual HTTP server with cpp-httplib
    // Endpoints:
    //   GET /health -> cbs.on_health()
    //   GET /status -> cbs.on_status()
    //   GET /models -> cbs.on_list_models()
    //   POST /models/{id}/activate -> cbs.on_activate_model(id)
    //   POST /models/upload -> cbs.on_upload_model(name, data)

    MOZART_LOG_INFO("HTTP server started on {}:{}", impl_->cfg.bind_addr, impl_->cfg.port);
}

void HttpServer::stop() {
    if (!impl_->running) return;
    impl_->running = false;
    MOZART_LOG_INFO("HTTP server stopped");
}

}  // namespace mozart
