#include "mozart/http_api.hpp"
#include <iostream>
#include <cstdlib>

using Json = nlohmann::json;

// 预设夹具按环境变量切换成功与失败形状，用于覆盖 HTTP 状态码映射。
static bool presets_available() {
    const char* mode = std::getenv("MOZART_FIXTURE_PRESETS");
    return mode != nullptr && std::string(mode) == "ok";
}

class TestControl final : public rvc::ControlPlane {
public:
    explicit TestControl(std::filesystem::path output) : output_(std::move(output)) {}
    Json request_mode(const std::string&, const std::string&) override { return {{"status", "switching_deferred"}}; }
    Json set_realtime_routing(const Json&) override { return {}; }
    Json enqueue_file(std::filesystem::path, const std::string&, const std::string&) override { return {}; }
    Json job_status(const std::string&) const override { return {}; }
    std::optional<std::filesystem::path> completed_output(const std::string&) const override { return output_; }
    Json cancel_job(const std::string&) override { return {}; }
    Json pause_file_queue() override { return {}; }
    Json resume_file_queue() override { return {}; }
    Json remove_job(const std::string&) override { return {}; }
    Json clear_finished_jobs() override { return {}; }
    Json status() const override { return {{"mode", "file_rvc"}}; }
    Json logs(size_t) const override { return {}; }
    Json parameters() const override { return {}; }
    Json set_parameters(const Json&) override { return {}; }
    Json reset_parameters() override { return {}; }
    Json list_presets() const override {
        if (presets_available()) return {{"presets", Json::array()}};
        return {{"status", "failed"}, {"error", "preset file is corrupt"}};
    }
    Json save_preset(const Json&) override {
        if (presets_available()) return {{"status", "saved"}, {"id", "new"}};
        return {{"status", "failed"}, {"error", "preset file is corrupt"}};
    }
    Json delete_preset(const std::string& id) override {
        if (!presets_available()) return {{"status", "failed"}, {"error", "preset file is corrupt"}};
        if (id == "missing") return {{"error", "preset not found"}};
        return {{"status", "deleted"}};
    }
    Json list_models() const override { return {}; }
    std::filesystem::path upload_path(const std::string&) const override { return output_.parent_path() / "upload"; }
    nlohmann::json preprocessor_status() const override { return {{"available", false}}; }
    nlohmann::json set_preprocessor(const nlohmann::json&) override { return {{"status", "failed"}}; }
private:
    std::filesystem::path output_;
};

int main(int argc, char** argv) {
    if (argc != 3) return 2;
    TestControl controller(argv[2]);
    rvc::HttpApiServer::Limits limits;
    limits.connections = 8;
    limits.subtitle_connections = 2;
    limits.request_memory_bytes = 32 * 1024;
    limits.header_timeout = std::chrono::milliseconds(600);
    limits.request_timeout = std::chrono::milliseconds(1000);
    limits.write_timeout = std::chrono::milliseconds(1000);
    rvc::HttpApiServer server("127.0.0.1", static_cast<uint16_t>(std::atoi(argv[1])), &controller, limits);
    if (!server.start()) return 1;
    std::cin.get();
    server.stop();
}
