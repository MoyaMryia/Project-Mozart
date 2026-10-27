#include "mozart/control_api.hpp"
#include <iostream>
#include <cstdlib>

using Json = nlohmann::json;

// 与 http_fixture 相同的桩表面；status 携带 mode/ready 供 STATUS 映射验证。
class TestControl final : public rvc::ControlPlane {
public:
    Json request_mode(const std::string& mode, const std::string&) override {
        last_mode_ = mode;
        return {{"ok", true}, {"mode", mode}};
    }
    Json set_realtime_routing(const Json&) override { return {}; }
    Json enqueue_file(std::filesystem::path, const std::string&, const std::string&) override { return {}; }
    Json job_status(const std::string&) const override { return {}; }
    std::optional<std::filesystem::path> completed_output(const std::string&) const override { return {}; }
    Json cancel_job(const std::string&) override { return {}; }
    Json pause_file_queue() override { return {}; }
    Json resume_file_queue() override { return {}; }
    Json remove_job(const std::string&) override { return {}; }
    Json clear_finished_jobs() override { return {}; }
    Json status() const override {
        return {{"mode", last_mode_}, {"ready", true}, {"jobs_finished", 7}, {"jobs_failed", 2}};
    }
    Json logs(size_t) const override { return {}; }
    Json parameters() const override { return {}; }
    Json set_parameters(const Json&) override { return {}; }
    Json reset_parameters() override { return {}; }
    Json list_presets() const override { return {}; }
    Json save_preset(const Json&) override { return {}; }
    Json delete_preset(const std::string&) override { return {}; }
    Json list_models() const override { return {}; }
    std::filesystem::path upload_path(const std::string&) const override { return {}; }
    nlohmann::json preprocessor_status() const override { return {{"available", false}}; }
    nlohmann::json set_preprocessor(const nlohmann::json&) override { return {{"status", "failed"}}; }
private:
    std::string last_mode_ = "realtime";
};

int main(int argc, char** argv) {
    if (argc != 2) return 2;
    TestControl controller;
    rvc::ControlApiServer server(argv[1], &controller);
    if (!server.start()) return 1;
    std::cin.get();
    server.stop();
}
