#include "mozart/control_api.hpp"
#include "mozart/ctlmsg.h"
#include "state/mode_controller.hpp"
#include <chrono>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <thread>

namespace {
void require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
struct Pipeline : rvc::MockRVCPipeline {
    std::atomic<bool> blocked{false}, entered{false};
    std::string current_model_id() const override { return "fixture"; }
    std::vector<float> process(const std::vector<float>& input) override {
        entered = true;
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
        while (blocked && std::chrono::steady_clock::now() < deadline)
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        if (blocked) throw std::runtime_error("test inference gate timed out");
        return MockRVCPipeline::process(input);
    }
};
struct Directory {
    std::filesystem::path path;
    Directory() {
        char pattern[] = "/tmp/mozart-protocol-test-XXXXXX";
        auto root = mkdtemp(pattern);
        require(root != nullptr, "temporary directory creation failed");
        path = root;
    }
    ~Directory() { std::filesystem::remove_all(path); }
};
void wav(const std::filesystem::path& path) {
    std::ofstream out(path, std::ios::binary);
    const auto little = [&](unsigned value, unsigned bytes) {
        for (unsigned i = 0; i < bytes; ++i) out.put(static_cast<char>(value >> (8 * i)));
    };
    out.write("RIFF", 4); little(36 + 640, 4); out.write("WAVEfmt ", 8);
    little(16, 4); little(1, 2); little(1, 2); little(16000, 4); little(32000, 4);
    little(2, 2); little(16, 2); out.write("data", 4); little(640, 4);
    for (int i = 0; i < 320; ++i) little(1000, 2);
}
template<class Predicate> void wait(Predicate predicate) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (!predicate()) {
        require(std::chrono::steady_clock::now() < deadline, "controller operation timed out");
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
}
uint32_t number(const std::vector<unsigned char>& reply, size_t at) {
    uint32_t value; std::memcpy(&value, reply.data() + at, 4); return value;
}
}
int main() {
    try {
        Directory root;
        Pipeline pipeline;
        rvc::ModeController::Config cfg;
        cfg.audio_host = "127.0.0.1";
        cfg.audio_port = 0;
        cfg.storage_dir = root.path / "jobs";
        cfg.presets_path = root.path / "presets.json";
        rvc::ModeController controller(pipeline, cfg);
        struct Release { Pipeline& pipeline; ~Release() { pipeline.blocked = false; } } release{pipeline};
        const auto request = [&](uint8_t type, int mode = -1) {
            std::vector<unsigned char> data{MOZART_CTL_VERSION, type, 1, 0, 42, 0, 0, 0};
            if (mode >= 0) data.push_back(static_cast<unsigned char>(mode));
            auto reply = rvc::control_reply(controller, data.data(), data.size());
            require(reply.size() >= MOZART_CTL_HEADER_BYTES && number(reply, 4) == 42,
                    "protocol response lost its request ID");
            return reply;
        };
        require(request(MOZART_CTL_PING)[1] == MOZART_CTL_PONG, "PING was not acknowledged");
        for (unsigned mode : {MOZART_CTL_MODE_REALTIME, MOZART_CTL_MODE_FILE, MOZART_CTL_MODE_IDLE}) {
            const auto reply = request(MOZART_CTL_MODE_SET, mode);
            require(reply[1] == MOZART_CTL_MODE_EVT && reply[8] == mode, "real controller rejected a supported mode");
            const auto status = request(MOZART_CTL_STATUS);
            require(status[8] == mode, "binary status disagrees with the controller mode");
            require(status[9] == (mode == MOZART_CTL_MODE_IDLE ? MOZART_CTL_STATE_DOWN : MOZART_CTL_STATE_READY),
                    "binary status disagrees with worker readiness");
        }
        require(request(MOZART_CTL_MODE_SET, 255)[1] == MOZART_CTL_ERR, "unknown mode was accepted");
        require(controller.status()["mode"] == "idle", "invalid mode changed the active mode");
        require(request(MOZART_CTL_MODE_SET)[1] == MOZART_CTL_ERR, "missing mode was accepted");
        unsigned char extra_body[10]{MOZART_CTL_VERSION, MOZART_CTL_MODE_SET, 1, 0, 42, 0, 0, 0, MOZART_CTL_MODE_FILE, 0};
        require(rvc::control_reply(controller, extra_body, sizeof(extra_body))[1] == MOZART_CTL_ERR,
                "extra mode bytes were accepted");
        require(controller.status()["mode"] == "idle", "extra bytes changed the active mode");
        unsigned char bad_version[8]{99, MOZART_CTL_PING, 1, 0, 42, 0, 0, 0};
        require(rvc::control_reply(controller, bad_version, 8)[1] == MOZART_CTL_ERR, "bad version was accepted");
        require(rvc::control_reply(controller, bad_version, 7).empty(), "short header received a response");
        require(request(99)[1] == MOZART_CTL_ERR, "unknown command was accepted");

        wav(cfg.storage_dir / "good.wav");
        const auto good = controller.enqueue_file(cfg.storage_dir / "good.wav", "good.wav", "").at("job_id").get<std::string>();
        const auto bad = controller.enqueue_file(cfg.storage_dir / "missing.wav", "missing.wav", "").at("job_id").get<std::string>();
        request(MOZART_CTL_MODE_SET, MOZART_CTL_MODE_FILE);
        wait([&] { return controller.job_status(good)["status"] == "completed"
                       && controller.job_status(bad)["status"] == "failed"; });
        auto status = request(MOZART_CTL_STATUS);
        require(number(status, 10) == 1 && number(status, 14) == 1, "queue counters were not encoded");

        pipeline.entered = false;
        pipeline.blocked = true;
        wav(cfg.storage_dir / "active.wav");
        controller.enqueue_file(cfg.storage_dir / "active.wav", "active.wav", "");
        wait([&] { return pipeline.entered.load(); });
        auto deferred = request(MOZART_CTL_MODE_SET, MOZART_CTL_MODE_IDLE);
        require(deferred[1] == MOZART_CTL_MODE_EVT && deferred[9] == MOZART_CTL_STATE_STARTING,
                "deferred transition was reported as a failure");
        status = request(MOZART_CTL_STATUS);
        require(status[8] == MOZART_CTL_MODE_FILE && status[9] == MOZART_CTL_STATE_STARTING,
                "pending transition was not observable");
        pipeline.blocked = false;
        wait([&] { return controller.status()["mode"] == "idle"; });
        status = request(MOZART_CTL_STATUS);
        require(status[8] == MOZART_CTL_MODE_IDLE && number(status, 10) == 2,
                "deferred transition or final counters were lost");
        auto disabled_cfg = cfg;
        disabled_cfg.rvc_enabled = false;
        rvc::ModeController disabled(pipeline, disabled_cfg);
        unsigned char file_request[9]{MOZART_CTL_VERSION, MOZART_CTL_MODE_SET, 1, 0, 42, 0, 0, 0, MOZART_CTL_MODE_FILE};
        require(rvc::control_reply(disabled, file_request, sizeof(file_request))[1] == MOZART_CTL_ERR,
                "disabled RVC was reported as active");
        std::cout << "control protocol integration passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
