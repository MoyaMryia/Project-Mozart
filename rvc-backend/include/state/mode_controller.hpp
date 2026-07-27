#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

#include "rvc/pipeline.hpp"
#include "state/control_plane.hpp"
#include "state/file_rvc_worker.hpp"
#include "state/realtime_rvc_worker.hpp"

namespace rvc {

// The control plane owns lifecycles and job scheduling only. Audio samples stay
// inside AudioWorker and RVCPipeline, so mode changes cannot race inference.
class ModeController final : public ControlPlane {
public:
    struct Config {
        std::string audio_host;
        uint16_t audio_port = 18000;
        uint32_t input_sample_rate = MOZART_INPUT_SAMPLE_RATE;
        uint32_t output_sample_rate = MOZART_OUTPUT_SAMPLE_RATE;
        uint32_t frame_duration_ms = MOZART_INPUT_FRAME_MS;
        bool skip_silence = true;
        std::filesystem::path storage_dir = "./storage/temp";
        std::string ffmpeg_path = "ffmpeg";
        size_t max_queue_depth = 50;
        uint64_t max_cache_bytes = 1000ULL * 1024ULL * 1024ULL;
        std::filesystem::path models_dir = "./models";
        std::filesystem::path presets_path = "./storage/presets.json";
        RvcParameters default_parameters{};
    };

    ModeController(RVCPipelineBase& pipeline, Config config);
    ~ModeController();

    ModeController(const ModeController&) = delete;
    ModeController& operator=(const ModeController&) = delete;

    nlohmann::json request_mode(const std::string& mode,
                                const std::string& model_id = "") override;
    nlohmann::json enqueue_file(std::filesystem::path source_file,
                                const std::string& original_name,
                                const std::string& model_id) override;
    nlohmann::json job_status(const std::string& job_id) const override;
    std::optional<std::filesystem::path> completed_output(const std::string& job_id) const override;
    nlohmann::json cancel_job(const std::string& job_id) override;
    nlohmann::json pause_file_queue() override;
    nlohmann::json resume_file_queue() override;
    nlohmann::json remove_job(const std::string& job_id) override;
    nlohmann::json clear_finished_jobs() override;
    nlohmann::json status() const override;
    nlohmann::json logs(size_t limit) const override;
    nlohmann::json parameters() const override;
    nlohmann::json set_parameters(const nlohmann::json& parameters) override;
    nlohmann::json reset_parameters() override;
    nlohmann::json list_presets() const override;
    nlohmann::json save_preset(const nlohmann::json& preset) override;
    nlohmann::json delete_preset(const std::string& preset_id) override;
    nlohmann::json list_models() const override;
    std::filesystem::path upload_path(const std::string& original_name) const override;

private:
    struct Job {
        std::string id;
        std::string name;
        std::string model_id;
        std::filesystem::path source_path;
        std::filesystem::path output_path;
        std::string state = "queued";
        int progress = 0;
        std::string error;
        std::shared_ptr<std::atomic<bool>> cancel_requested = std::make_shared<std::atomic<bool>>(false);
    };

    RVCPipelineBase& pipeline_;
    Config config_;
    mutable std::mutex mutex_;
    std::condition_variable jobs_changed_;
    std::deque<Job> jobs_;
    std::unique_ptr<RealtimeRvcWorker> realtime_worker_;
    std::unique_ptr<FileRvcWorker> file_worker_;
    std::thread file_thread_;
    bool shutting_down_ = false;
    std::string mode_ = "idle";
    std::string pending_mode_;
    std::string last_error_;
    bool file_queue_paused_ = false;

    void start_realtime_locked();
    void stop_realtime_locked();
    nlohmann::json transition_locked(const std::string& mode, const std::string& model_id);
    void file_loop();
    void process_job(const std::string& job_id);
    void evict_cache();
    static bool supported_mode(const std::string& mode);
    static bool unavailable_mode(const std::string& mode);
    static std::string make_job_id();
    static std::string sanitize_extension(const std::string& name);
    nlohmann::json job_json(const Job& job) const;
    static nlohmann::json parameters_json(const RvcParameters& parameters);
    bool save_presets_locked(const nlohmann::json& presets) const;
    nlohmann::json read_presets_locked() const;
};

} // namespace rvc
