#pragma once

#include <memory>

#include "utils/config.hpp"

namespace rvc {
class HttpApiServer;
class ModeController;
class RVCPipelineBase;
}

namespace mozart {

// Process-level composition root. It is the sole owner that wires the control
// plane to IO, preprocessing, RVC workers, and the HTTP gateway.
class StateManagerDaemon {
public:
    explicit StateManagerDaemon(rvc::Config config);
    ~StateManagerDaemon();

    StateManagerDaemon(const StateManagerDaemon&) = delete;
    StateManagerDaemon& operator=(const StateManagerDaemon&) = delete;

    bool start();
    void stop();

private:
    rvc::Config config_;
    std::unique_ptr<rvc::RVCPipelineBase> pipeline_;
    std::unique_ptr<rvc::ModeController> controller_;
    std::unique_ptr<rvc::HttpApiServer> api_;
    bool started_ = false;
};

} // namespace mozart
