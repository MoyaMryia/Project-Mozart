#include "state/mode_controller.hpp"

#include <algorithm>
#include <chrono>
#include <fstream>
#include <stdexcept>
#include <utility>

#include <spdlog/spdlog.h>

#include "state/log_store.hpp"

namespace rvc {
ModeController::ModeController(RVCPipelineBase& pipeline, Config config)
    : pipeline_(pipeline), config_(std::move(config)) {
    std::filesystem::create_directories(config_.storage_dir);
    if (!std::filesystem::exists(config_.presets_path)) save_presets_locked(nlohmann::json::array());
    FileRvcWorker::Config file_config;
    file_config.storage_dir = config_.storage_dir;
    file_config.ffmpeg_path = config_.ffmpeg_path;
    file_config.output_sample_rate = config_.output_sample_rate;
    file_config.rnnoise = config_.file_rnnoise;
    file_worker_ = std::make_unique<FileRvcWorker>(pipeline_, std::move(file_config));
    file_thread_ = std::thread(&ModeController::file_loop, this);
}

ModeController::~ModeController() {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        shutting_down_ = true;
        stop_realtime_locked();
    }
    jobs_changed_.notify_all();
    if (file_thread_.joinable()) file_thread_.join();
}

bool ModeController::supported_mode(const std::string& mode) {
    return mode == "idle" || mode == "rt_rvc" || mode == "file_rvc";
}

bool ModeController::unavailable_mode(const std::string& mode) {
    return mode == "rt_zero_shot" || mode == "file_zero_shot";
}

std::string ModeController::make_job_id() {
    const auto now = std::chrono::time_point_cast<std::chrono::microseconds>(
        std::chrono::system_clock::now()).time_since_epoch().count();
    return "job_" + std::to_string(now);
}

std::string ModeController::sanitize_extension(const std::string& name) {
    const auto extension = std::filesystem::path(name).extension().string();
    const std::vector<std::string> allowed = {
        ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"
    };
    return std::find(allowed.begin(), allowed.end(), extension) != allowed.end()
        ? extension : ".bin";
}

void ModeController::start_realtime_locked() {
    if (realtime_worker_ && realtime_worker_->running()) return;

    RealtimeRvcWorker::Config worker_config;
    worker_config.host = config_.audio_host;
    worker_config.port = config_.audio_port;
    worker_config.input_sample_rate = config_.input_sample_rate;
    worker_config.output_sample_rate = config_.output_sample_rate;
    worker_config.frame_duration_ms = config_.frame_duration_ms;
    worker_config.skip_silence = config_.skip_silence;
    realtime_worker_ = std::make_unique<RealtimeRvcWorker>(pipeline_, std::move(worker_config));
    realtime_worker_->start();
}

void ModeController::stop_realtime_locked() {
    if (realtime_worker_) {
        realtime_worker_->stop();
        realtime_worker_.reset();
    }
}

nlohmann::json ModeController::transition_locked(const std::string& mode,
                                                 const std::string& model_id) {
    // A model swap must never overlap an active audio or file inference call.
    const std::string previous_mode = mode_;
    stop_realtime_locked();
    const bool switch_model = !model_id.empty() && model_id != pipeline_.current_model_id();
    if (switch_model && !pipeline_.is_mock() && !pipeline_.switch_model(model_id)) {
        if (previous_mode == "rt_rvc") {
            try {
                start_realtime_locked();
            } catch (const std::exception& error) {
                mode_ = "idle";
                last_error_ = error.what();
            }
        }
        return {{"status", "failed"}, {"error", "model not found or failed to load"}};
    }

    mode_ = mode;
    if (mode_ == "file_rvc") file_queue_paused_ = false;
    last_error_.clear();
    if (mode == "rt_rvc") {
        try {
            start_realtime_locked();
        } catch (const std::exception& error) {
            mode_ = "idle";
            last_error_ = error.what();
            return {{"status", "failed"}, {"error", last_error_}};
        }
    }
    jobs_changed_.notify_all();
    if (mode_ == "idle") {
        if (previous_mode != "idle") spdlog::info("[{}] mode deactivated", previous_mode);
        spdlog::info("[system] global processing stopped");
    } else {
        spdlog::info("[{}] mode activated{}", mode_,
                     switch_model ? " after model switch" : "");
    }
    return {{"status", "active"}, {"mode", mode_}, {"model_id", pipeline_.current_model_id()}};
}

nlohmann::json ModeController::request_mode(const std::string& mode, const std::string& model_id) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (unavailable_mode(mode)) {
        return {{"status", "unavailable"}, {"mode", mode},
                {"error", "Zero-Shot worker is not implemented"}};
    }
    if (!supported_mode(mode)) {
        return {{"status", "invalid"}, {"error", "unsupported mode"}};
    }
    const auto active = std::find_if(jobs_.begin(), jobs_.end(), [](const Job& job) {
        return job.state == "processing";
    });
    if (mode_ == "file_rvc" && active != jobs_.end() && mode != "file_rvc") {
        pending_mode_ = mode;
        return {{"status", "switching_deferred"}, {"current_active_job", active->id},
                {"target_mode", mode}};
    }
    if (active != jobs_.end() && !model_id.empty()) {
        return {{"status", "busy"}, {"error", "model changes wait until the active file job completes"},
                {"current_active_job", active->id}};
    }
    return transition_locked(mode, model_id);
}

nlohmann::json ModeController::enqueue_file(std::filesystem::path source_file,
                                            const std::string& original_name,
                                            const std::string& model_id) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (jobs_.size() >= config_.max_queue_depth) {
        return {{"status", "rejected"}, {"error", "file queue is full"}};
    }
    if (!pipeline_.is_mock() && model_id.empty() && pipeline_.current_model_id().empty()) {
        return {{"status", "rejected"}, {"error", "an RVC model must be selected"}};
    }
    if (!pipeline_.is_mock() && !model_id.empty() && model_id != pipeline_.current_model_id()) {
        const auto models = list_models();
        const auto model = std::find_if(models["models"].begin(), models["models"].end(), [&](const auto& item) {
            return item.value("id", "") == model_id && item.value("exists", false);
        });
        if (model == models["models"].end()) return {{"status", "rejected"}, {"error", "selected RVC model is unavailable"}};
    }
    Job job;
    job.id = make_job_id();
    job.name = original_name;
    job.model_id = model_id;
    job.source_path = std::move(source_file);
    job.output_path = config_.storage_dir / (job.id.substr(4) + "_output.wav");
    jobs_.push_back(job);
    jobs_changed_.notify_all();
    return {{"job_id", job.id}, {"status", "queued"}, {"queue_position", jobs_.size()}};
}

nlohmann::json ModeController::job_json(const Job& job) const {
    nlohmann::json json = {
        {"job_id", job.id}, {"name", job.name}, {"mode", "file_rvc"},
        {"status", job.state}, {"progress", job.progress}, {"error", job.error}
    };
    if (job.state == "completed") json["download_url"] = "/api/file/result?job_id=" + job.id;
    return json;
}

nlohmann::json ModeController::job_status(const std::string& job_id) const {
    std::lock_guard<std::mutex> lock(mutex_);
    const auto job = std::find_if(jobs_.begin(), jobs_.end(), [&](const Job& item) {
        return item.id == job_id;
    });
    return job == jobs_.end() ? nlohmann::json{{"error", "job not found"}} : job_json(*job);
}

std::optional<std::filesystem::path> ModeController::completed_output(const std::string& job_id) const {
    std::lock_guard<std::mutex> lock(mutex_);
    const auto job = std::find_if(jobs_.begin(), jobs_.end(), [&](const Job& item) {
        return item.id == job_id;
    });
    if (job == jobs_.end() || job->state != "completed" || !std::filesystem::exists(job->output_path)) return std::nullopt;
    return job->output_path;
}

nlohmann::json ModeController::cancel_job(const std::string& job_id) {
    std::lock_guard<std::mutex> lock(mutex_);
    const auto job = std::find_if(jobs_.begin(), jobs_.end(), [&](const Job& item) {
        return item.id == job_id;
    });
    if (job == jobs_.end()) return {{"error", "job not found"}};
    if (job->state == "queued") {
        job->state = "cancelled";
        spdlog::info("[file_rvc] queued job cancelled: {}", job->id);
    } else if (job->state == "processing") {
        job->cancel_requested->store(true);
        job->state = "cancelling";
        spdlog::info("[file_rvc] cancel requested for active job: {}", job->id);
    }
    else return {{"error", "job cannot be cancelled"}};
    jobs_changed_.notify_all();
    return job_json(*job);
}

nlohmann::json ModeController::pause_file_queue() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (mode_ != "file_rvc") return {{"status", "invalid"}, {"error", "FILE_RVC is not active"}};
    file_queue_paused_ = true;
    spdlog::info("[file_rvc] queue paused; active job will finish");
    return {{"status", "paused"}};
}

nlohmann::json ModeController::resume_file_queue() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (mode_ != "file_rvc") return {{"status", "invalid"}, {"error", "FILE_RVC is not active"}};
    file_queue_paused_ = false;
    jobs_changed_.notify_all();
    spdlog::info("[file_rvc] queue resumed");
    return {{"status", "active"}};
}

nlohmann::json ModeController::remove_job(const std::string& job_id) {
    std::lock_guard<std::mutex> lock(mutex_);
    const auto job = std::find_if(jobs_.begin(), jobs_.end(), [&](const Job& item) { return item.id == job_id; });
    if (job == jobs_.end()) return {{"error", "job not found"}};
    if (job->state == "queued" || job->state == "processing") return {{"error", "cancel the job before removing it"}};
    std::error_code error;
    std::filesystem::remove(job->source_path, error);
    std::filesystem::remove(job->output_path, error);
    spdlog::info("[file_rvc] job removed: {}", job->id);
    jobs_.erase(job);
    return {{"status", "removed"}};
}

nlohmann::json ModeController::clear_finished_jobs() {
    std::lock_guard<std::mutex> lock(mutex_);
    size_t removed = 0;
    for (auto job = jobs_.begin(); job != jobs_.end();) {
        if (job->state == "queued" || job->state == "processing") { ++job; continue; }
        std::error_code error;
        std::filesystem::remove(job->source_path, error);
        std::filesystem::remove(job->output_path, error);
        job = jobs_.erase(job);
        ++removed;
    }
    spdlog::info("[file_rvc] cleared {} finished jobs", removed);
    return {{"status", "cleared"}, {"count", removed}};
}

nlohmann::json ModeController::status() const {
    std::lock_guard<std::mutex> lock(mutex_);
    nlohmann::json queue = nlohmann::json::array();
    for (const auto& job : jobs_) queue.push_back(job_json(job));
    nlohmann::json model = pipeline_.model_info();
    const auto vad = realtime_worker_ ? realtime_worker_->vad_stats() : AudioWorker::VadStats{};
    const auto latency = realtime_worker_ ? realtime_worker_->latency_stats() : AudioWorker::LatencyStats{};
    const auto bypass = realtime_worker_ ? realtime_worker_->bypass_stats() : AudioWorker::BypassStats{};
    const auto stream = realtime_worker_ ? realtime_worker_->stream_stats() : AudioWorker::StreamStats{};
    return {
        {"mode", mode_}, {"pending_target_mode", pending_mode_.empty() ? nlohmann::json(nullptr) : nlohmann::json(pending_mode_)},
        {"worker_running", realtime_worker_ && realtime_worker_->running()},
        {"stream_mode", realtime_worker_ && realtime_worker_->is_stream_mode()},
        {"pipeline_mode", pipeline_.is_mock() ? "mock" : "real"},
        {"active_model_id", pipeline_.current_model_id()}, {"model", model},
        {"vad", {{"available", realtime_worker_ && realtime_worker_->running()}, {"frame_count", vad.frame_count},
                 {"voiced_percent", vad.frame_count == 0 ? 0.0 : 100.0 * vad.voiced_frame_count / vad.frame_count},
                 {"confidence_percent", vad.confidence_percent}}},
        {"latency", {{"available", realtime_worker_ && realtime_worker_->running()}, {"count", latency.count},
                     {"avg_ms", latency.avg_ms}, {"max_ms", latency.max_ms}}},
        {"bypass", {{"inference_count", bypass.inference_count}, {"bypass_count", bypass.bypass_count}}},
        {"stream", {{"blocks", stream.blocks}, {"skipped_blocks", stream.skipped_blocks},
                    {"resets", stream.resets}, {"late_blocks", stream.late_blocks},
                    {"input_overruns", stream.input_overruns}, {"output_overruns", stream.output_overruns},
                    {"inference_errors", stream.inference_errors}, {"output_underruns", stream.output_underruns},
                    {"startup_output_underruns", stream.startup_output_underruns}}},
        {"queue", queue}, {"file_queue_paused", file_queue_paused_}, {"last_error", last_error_},
        {"capabilities", { {"rt_rvc", true}, {"file_rvc", true}, {"rt_zero_shot", false}, {"file_zero_shot", false} }}
    };
}

nlohmann::json ModeController::logs(size_t limit) const {
    nlohmann::json result;
    result["entries"] = nlohmann::json::array();
    for (const auto& entry : recent_backend_logs(std::min(limit, size_t{1000}))) {
        std::string mode = "system";
        if (entry.message.size() > 2 && entry.message.front() == '[') {
            const auto end = entry.message.find(']');
            if (end != std::string::npos) mode = entry.message.substr(1, end - 1);
        }
        result["entries"].push_back({
            {"timestamp", entry.timestamp}, {"level", entry.level}, {"mode", mode}, {"message", entry.message}
        });
    }
    return result;
}

nlohmann::json ModeController::parameters() const {
    return parameters_json(pipeline_.parameters());
}

nlohmann::json ModeController::parameters_json(const RvcParameters& parameters) {
    return {
        {"f0_method", parameters.f0_method}, {"pitch_shift", parameters.pitch_shift},
        {"index_rate", parameters.index_rate}, {"filter_radius", parameters.filter_radius},
        {"rms_mix_rate", parameters.rms_mix_rate}, {"protect", parameters.protect}
    };
}

nlohmann::json ModeController::set_parameters(const nlohmann::json& input) {
    std::lock_guard<std::mutex> lock(mutex_);
    const auto active = std::find_if(jobs_.begin(), jobs_.end(), [](const Job& job) {
        return job.state == "processing";
    });
    if (active != jobs_.end()) {
        return {{"status", "busy"}, {"error", "parameters cannot change during an active file job"}};
    }
    RvcParameters parameters = pipeline_.parameters();
    parameters.f0_method = input.value("f0_method", parameters.f0_method);
    parameters.pitch_shift = input.value("pitch_shift", parameters.pitch_shift);
    parameters.index_rate = input.value("index_rate", parameters.index_rate);
    parameters.filter_radius = input.value("filter_radius", parameters.filter_radius);
    parameters.rms_mix_rate = input.value("rms_mix_rate", parameters.rms_mix_rate);
    parameters.protect = input.value("protect", parameters.protect);
    if (!pipeline_.set_parameters(parameters)) {
        return {{"status", "invalid"}, {"error", "parameters are outside the supported RVC range"}};
    }
    spdlog::info("[rvc] parameters updated: f0={}, pitch={}, index_rate={}, filter_radius={}, rms_mix_rate={}, protect={}",
                 parameters.f0_method, parameters.pitch_shift, parameters.index_rate,
                 parameters.filter_radius, parameters.rms_mix_rate, parameters.protect);
    return {{"status", "active"}, {"parameters", this->parameters()}};
}

nlohmann::json ModeController::reset_parameters() {
    return set_parameters(parameters_json(config_.default_parameters));
}

nlohmann::json ModeController::read_presets_locked() const {
    std::ifstream input(config_.presets_path);
    if (!input) return nlohmann::json::array();
    try {
        auto presets = nlohmann::json::parse(input);
        return presets.is_array() ? presets : nlohmann::json::array();
    } catch (...) {
        return nlohmann::json::array();
    }
}

bool ModeController::save_presets_locked(const nlohmann::json& presets) const {
    const auto temporary = config_.presets_path.string() + ".tmp";
    std::ofstream output(temporary, std::ios::trunc);
    if (!output) return false;
    output << presets.dump(2);
    output.close();
    std::error_code error;
    std::filesystem::rename(temporary, config_.presets_path, error);
    if (error) {
        std::filesystem::remove(config_.presets_path, error);
        error.clear();
        std::filesystem::rename(temporary, config_.presets_path, error);
    }
    return !error;
}

nlohmann::json ModeController::list_presets() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return {{"presets", read_presets_locked()}};
}

nlohmann::json ModeController::save_preset(const nlohmann::json& preset) {
    std::lock_guard<std::mutex> lock(mutex_);
    const std::string name = preset.value("name", "");
    if (name.empty() || name.size() > 80) return {{"status", "invalid"}, {"error", "preset name is required"}};
    const auto parameters = preset.value("parameters", nlohmann::json::object());
    RvcParameters validated = pipeline_.parameters();
    validated.f0_method = parameters.value("f0_method", validated.f0_method);
    validated.pitch_shift = parameters.value("pitch_shift", validated.pitch_shift);
    validated.index_rate = parameters.value("index_rate", validated.index_rate);
    validated.filter_radius = parameters.value("filter_radius", validated.filter_radius);
    validated.rms_mix_rate = parameters.value("rms_mix_rate", validated.rms_mix_rate);
    validated.protect = parameters.value("protect", validated.protect);
    const bool valid = (validated.f0_method == "rmvpe" || validated.f0_method == "harvest" || validated.f0_method == "pm")
        && validated.pitch_shift >= -12 && validated.pitch_shift <= 12
        && validated.index_rate >= 0.0f && validated.index_rate <= 1.0f
        && validated.filter_radius >= 0 && validated.filter_radius <= 7
        && validated.rms_mix_rate >= 0.0f && validated.rms_mix_rate <= 1.0f
        && validated.protect >= 0.0f && validated.protect <= 0.5f;
    if (!valid) return {{"status", "invalid"}, {"error", "preset parameters are invalid"}};
    auto presets = read_presets_locked();
    const std::string id = "preset_" + std::to_string(std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count());
    presets.push_back({{"id", id}, {"name", name}, {"parameters", parameters_json(validated)}});
    if (!save_presets_locked(presets)) return {{"status", "failed"}, {"error", "failed to save preset"}};
    spdlog::info("[rvc] preset saved: {}", name);
    return {{"status", "saved"}, {"id", id}};
}

nlohmann::json ModeController::delete_preset(const std::string& preset_id) {
    std::lock_guard<std::mutex> lock(mutex_);
    auto presets = read_presets_locked();
    const auto preset = std::find_if(presets.begin(), presets.end(), [&](const auto& item) { return item.value("id", "") == preset_id; });
    if (preset == presets.end()) return {{"error", "preset not found"}};
    presets.erase(preset);
    if (!save_presets_locked(presets)) return {{"status", "failed"}, {"error", "failed to delete preset"}};
    spdlog::info("[rvc] preset deleted: {}", preset_id);
    return {{"status", "deleted"}};
}

nlohmann::json ModeController::list_models() const {
    nlohmann::json result;
    result["models"] = nlohmann::json::array();
    if (!std::filesystem::exists(config_.models_dir)) return result;
    for (const auto& entry : std::filesystem::directory_iterator(config_.models_dir)) {
        if (!entry.is_directory()) continue;
        const std::string id = entry.path().filename().string();
        const bool has_config = std::filesystem::exists(entry.path() / "config.json");
        const bool has_onnx = std::filesystem::exists(entry.path() / (id + ".onnx"));
        result["models"].push_back({
            {"id", id}, {"exists", has_config && has_onnx},
            {"loaded", pipeline_.current_model_id() == id},
            {"current", pipeline_.current_model_id() == id}
        });
    }
    return result;
}

std::filesystem::path ModeController::upload_path(const std::string& original_name) const {
    const auto now = std::chrono::time_point_cast<std::chrono::microseconds>(
        std::chrono::system_clock::now()).time_since_epoch().count();
    return config_.storage_dir / (std::to_string(now) + "_input" + sanitize_extension(original_name));
}

void ModeController::file_loop() {
    std::unique_lock<std::mutex> lock(mutex_);
    while (!shutting_down_) {
        jobs_changed_.wait(lock, [&] {
            return shutting_down_ || (mode_ == "file_rvc" && !file_queue_paused_ && std::any_of(jobs_.begin(), jobs_.end(), [](const Job& job) {
                return job.state == "queued";
            }));
        });
        if (shutting_down_) break;
        auto job = std::find_if(jobs_.begin(), jobs_.end(), [](const Job& item) { return item.state == "queued"; });
        if (job == jobs_.end()) continue;
        job->state = "processing";
        job->progress = 1;
        const std::string job_id = job->id;
        lock.unlock();
        process_job(job_id);
        lock.lock();
        if (!pending_mode_.empty()) {
            const std::string requested = std::exchange(pending_mode_, "");
            transition_locked(requested, "");
        }
    }
}

void ModeController::process_job(const std::string& job_id) {
    Job snapshot;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        const auto job = std::find_if(jobs_.begin(), jobs_.end(), [&](const Job& item) { return item.id == job_id; });
        if (job == jobs_.end()) return;
        snapshot = *job;
    }
    FileRvcWorker::Request request;
    request.id = snapshot.id;
    request.model_id = snapshot.model_id;
    request.source_path = snapshot.source_path;
    request.output_path = snapshot.output_path;
    std::string processing_error;
    spdlog::info("[file_rvc] job {} started: {}", snapshot.id, snapshot.name);
    const bool succeeded = file_worker_->process(
        request,
        *snapshot.cancel_requested,
        [&](int progress) {
            std::lock_guard<std::mutex> lock(mutex_);
            const auto job = std::find_if(jobs_.begin(), jobs_.end(), [&](const Job& item) { return item.id == job_id; });
            if (job != jobs_.end()) job->progress = progress;
        },
        processing_error);

    {
        std::lock_guard<std::mutex> lock(mutex_);
        const auto job = std::find_if(jobs_.begin(), jobs_.end(), [&](const Job& item) { return item.id == job_id; });
        if (job == jobs_.end()) return;
        if (job->cancel_requested->load()) {
            job->state = "cancelled";
        } else if (!succeeded) {
            job->state = "failed";
            job->error = processing_error.empty() ? "offline RVC worker failed" : processing_error;
            spdlog::error("[file_rvc] job {} failed: {}", job->id, job->error);
        } else {
            job->state = "completed";
            job->progress = 100;
            spdlog::info("[file_rvc] job {} completed: {}", job->id, job->output_path.string());
        }
    }
    evict_cache();
}

void ModeController::evict_cache() {
    std::vector<std::filesystem::directory_entry> entries;
    uint64_t size = 0;
    for (const auto& entry : std::filesystem::directory_iterator(config_.storage_dir)) {
        if (!entry.is_regular_file()) continue;
        entries.push_back(entry);
        std::error_code error;
        size += entry.file_size(error);
    }
    if (size <= config_.max_cache_bytes) return;
    std::sort(entries.begin(), entries.end(), [](const auto& left, const auto& right) { return left.path().filename() < right.path().filename(); });
    const uint64_t target = config_.max_cache_bytes * 8 / 10;
    for (const auto& entry : entries) {
        if (size <= target) break;
        std::error_code error;
        const auto bytes = entry.file_size(error);
        std::filesystem::remove(entry.path(), error);
        if (!error) size -= bytes;
    }
}

} // namespace rvc
