#pragma once

#include <filesystem>
#include <optional>
#include <string>

#include <nlohmann/json.hpp>

namespace rvc {

// Transport-neutral command boundary. HTTP, WebSocket, Unix-socket, and CLI
// adapters use this interface; only state implements orchestration.
class ControlPlane {
public:
    virtual ~ControlPlane() = default;

    virtual nlohmann::json request_mode(const std::string& mode, const std::string& model_id) = 0;
    virtual nlohmann::json enqueue_file(std::filesystem::path source_file,
                                        const std::string& original_name,
                                        const std::string& model_id) = 0;
    virtual nlohmann::json job_status(const std::string& job_id) const = 0;
    virtual std::optional<std::filesystem::path> completed_output(const std::string& job_id) const = 0;
    virtual nlohmann::json cancel_job(const std::string& job_id) = 0;
    virtual nlohmann::json pause_file_queue() = 0;
    virtual nlohmann::json resume_file_queue() = 0;
    virtual nlohmann::json remove_job(const std::string& job_id) = 0;
    virtual nlohmann::json clear_finished_jobs() = 0;
    virtual nlohmann::json status() const = 0;
    virtual nlohmann::json logs(size_t limit) const = 0;
    virtual nlohmann::json parameters() const = 0;
    virtual nlohmann::json set_parameters(const nlohmann::json& parameters) = 0;
    virtual nlohmann::json reset_parameters() = 0;
    virtual nlohmann::json list_presets() const = 0;
    virtual nlohmann::json save_preset(const nlohmann::json& preset) = 0;
    virtual nlohmann::json delete_preset(const std::string& preset_id) = 0;
    virtual nlohmann::json list_models() const = 0;
    virtual std::filesystem::path upload_path(const std::string& original_name) const = 0;
};

} // namespace rvc
