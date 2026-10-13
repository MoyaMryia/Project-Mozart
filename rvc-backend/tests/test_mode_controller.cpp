#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <future>
#include <sys/wait.h>
#include <cerrno>

#include "state/mode_controller.hpp"

namespace {
namespace fs = std::filesystem;
using namespace std::chrono_literals;

void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}

// Only model inference is replaced. Tests use the real controller, file worker,
// FFmpeg decode/encode, and temporary files; no Jetson or model assets required.
class TestPipeline final : public rvc::RVCPipelineBase {
public:
    explicit TestPipeline(bool blocked, bool fail) : blocked_(blocked), fail_(fail) {}

    std::string current_model_id() const override { return "fixture"; }

    std::vector<float> process(const std::vector<float>& audio) override {
        std::unique_lock<std::mutex> lock(mutex_);
        entered_ = true;
        changed_.notify_all();
        if (!changed_.wait_for(lock, 10s, [&] { return !blocked_; })) {
            throw std::runtime_error("test inference release timed out");
        }
        if (fail_) throw std::runtime_error("intentional inference failure");
        return std::vector<float>(audio.size() * 3, 0.1f);
    }

    void wait_for_inference() {
        std::unique_lock<std::mutex> lock(mutex_);
        require(changed_.wait_for(lock, 10s, [&] { return entered_; }),
                "file worker did not reach inference");
    }

    void release() {
        std::lock_guard<std::mutex> lock(mutex_);
        blocked_ = false;
        changed_.notify_all();
    }

private:
    std::mutex mutex_;
    std::condition_variable changed_;
    bool blocked_;
    bool fail_;
    bool entered_ = false;
};

class TempDir {
public:
    TempDir() {
        auto pattern = (fs::temp_directory_path() / "mozart-queue-XXXXXX").string();
        std::vector<char> name(pattern.begin(), pattern.end());
        name.push_back('\0');
        const char* result = ::mkdtemp(name.data());
        require(result != nullptr, "could not create temporary test directory");
        path = result;
    }
    ~TempDir() { std::error_code error; fs::remove_all(path, error); }
    fs::path path;
};

void write_wav(const fs::path& path, unsigned samples) {
    std::ofstream out(path, std::ios::binary);
    const auto le = [&](unsigned value, unsigned bytes) {
        for (unsigned i = 0; i < bytes; ++i) out.put(static_cast<char>(value >> (8 * i)));
    };
    out.write("RIFF", 4); le(36 + samples * 2, 4); out.write("WAVEfmt ", 8);
    le(16, 4); le(1, 2); le(1, 2); le(16000, 4); le(32000, 4);
    le(2, 2); le(16, 2); out.write("data", 4); le(samples * 2, 4);
    for (unsigned i = 0; i < samples; ++i) le(1000, 2);
    out.close();
    require(static_cast<bool>(out), "could not write test WAV");
}

class Fixture {
public:
    Fixture(size_t depth = 50, uint64_t cache_bytes = 1024 * 1024,
            bool blocked = false, bool fail = false)
        : pipeline(blocked, fail) {
        rvc::ModeController::Config config;
        config.audio_host = "127.0.0.1";
        config.storage_dir = temporary.path / "audio";
        config.presets_path = temporary.path / "presets.json";
        config.models_dir = temporary.path / "models";
        config.max_queue_depth = depth;
        config.max_cache_bytes = cache_bytes;
        controller = std::make_unique<rvc::ModeController>(pipeline, config);
    }

    ~Fixture() {
        // Release the test gate before the controller joins the file thread,
        // including when an assertion throws.
        pipeline.release();
        controller.reset();
    }

    fs::path source(const std::string& name, unsigned samples = 320) const {
        auto path = temporary.path / "audio" / name;
        write_wav(path, samples);
        return path;
    }

    nlohmann::json enqueue(const fs::path& path) {
        return controller->enqueue_file(path, path.filename().string(), "");
    }

    void wait(const std::function<bool()>& predicate, const std::string& message) {
        const auto deadline = std::chrono::steady_clock::now() + 15s;
        while (!predicate()) {
            require(std::chrono::steady_clock::now() < deadline, message);
            std::this_thread::sleep_for(5ms);
        }
    }

    TempDir temporary;
    TestPipeline pipeline;
    std::unique_ptr<rvc::ModeController> controller;
};

std::string job_id(const nlohmann::json& result) {
    require(result.value("status", "") == "queued", "enqueue failed: " + result.dump());
    return result.at("job_id").get<std::string>();
}

void test_capacity() {
    Fixture f(2);
    const auto first = job_id(f.enqueue(f.source("first.wav")));
    job_id(f.enqueue(f.source("second.wav")));
    const auto third_path = f.source("third.wav");
    require(f.enqueue(third_path).value("status", "") == "rejected",
            "queued jobs must count towards the queue limit");
    require(f.controller->cancel_job(first).value("status", "") == "cancelled", "cancel failed");
    job_id(f.enqueue(third_path));
    require(f.controller->status().at("queue").size() == 3,
            "terminal history must not consume queue capacity");
}

void test_position() {
    Fixture f;
    require(f.enqueue(f.source("first.wav")).at("queue_position") == 1,
            "first queued job should have position 1");
    require(f.enqueue(f.source("second.wav")).at("queue_position") == 2,
            "second queued job should have position 2");
}

void test_clear_queued() {
    Fixture f;
    const auto pending_path = f.source("pending.wav");
    const auto cancelled_path = f.source("cancelled.wav");
    const auto pending = job_id(f.enqueue(pending_path));
    const auto cancelled = job_id(f.enqueue(cancelled_path));
    f.controller->cancel_job(cancelled);
    require(f.controller->clear_finished_jobs().at("count") == 1,
            "clear-finished must only remove terminal jobs");
    require(fs::exists(pending_path), "clear-finished deleted queued source audio");
    require(!fs::exists(cancelled_path), "cancelled source was not cleaned up");
    require(f.controller->job_status(pending).value("status", "") == "queued", "queued job was lost");
    require(f.controller->job_status(cancelled).contains("error"), "cancelled job was retained");
}

void test_active_cancel() {
    Fixture f(2, 1024 * 1024, true);
    const auto active = job_id(f.enqueue(f.source("active.wav")));
    require(f.controller->request_mode("file_rvc").value("status", "") == "active", "mode switch failed");
    f.pipeline.wait_for_inference();
    const auto pending_path = f.source("pending.wav");
    const auto pending = job_id(f.enqueue(pending_path));
    const auto third_path = f.source("third.wav");
    require(f.enqueue(third_path).value("status", "") == "rejected", "processing job must count towards limit");
    require(f.controller->cancel_job(active).value("status", "") == "cancelling", "active cancellation failed");
    require(f.enqueue(third_path).value("status", "") == "rejected", "cancelling job must still count towards limit");
    require(f.controller->clear_finished_jobs().at("count") == 0, "cleanup removed unfinished jobs");
    require(f.controller->set_parameters(nlohmann::json::object()).value("status", "") == "busy",
            "parameters must remain exclusive during cancellation");
    require(f.controller->request_mode("file_rvc", "another-model").value("status", "") == "busy",
            "model switches must remain exclusive during cancellation");
    require(f.controller->request_mode("idle").value("status", "") == "switching_deferred",
            "mode switch should wait for cancelling worker");
    f.pipeline.release();
    f.wait([&] { return f.controller->status().value("mode", "") == "idle"; }, "deferred idle never completed");
    require(f.controller->job_status(active).value("status", "") == "cancelled", "cancel did not finish");
    require(fs::exists(pending_path) && f.controller->job_status(pending).value("status", "") == "queued",
            "pending job must survive cancellation and mode switch");
    job_id(f.enqueue(third_path));
}

void test_eviction() {
    Fixture f(5, 1024, true);
    job_id(f.enqueue(f.source("100-active.wav")));
    f.controller->request_mode("file_rvc");
    f.pipeline.wait_for_inference();
    // Put queued audio first in the eviction order. It alone exceeds the cache
    // threshold: unfinished input must survive even when low-water is unreachable.
    const auto pending_path = f.source("001-pending.wav", 800);
    const auto pending = job_id(f.enqueue(pending_path));
    const auto orphan_path = f.source("000-orphan.wav", 800);
    require(f.controller->request_mode("idle").value("status", "") == "switching_deferred", "idle was not deferred");
    f.pipeline.release();
    // The deferred transition runs after process_job() and evict_cache().
    f.wait([&] { return f.controller->status().value("mode", "") == "idle"; }, "eviction did not finish");
    require(fs::exists(pending_path), "cache eviction deleted a queued input");
    require(f.controller->job_status(pending).value("status", "") == "queued", "pending job state changed");
    require(!fs::exists(orphan_path), "cache eviction did not remove unprotected files");
}

void test_clear_terminal(bool fail) {
    Fixture f(2, 1024 * 1024, false, fail);
    const auto finished_path = f.source("finished.wav");
    const auto finished = job_id(f.enqueue(finished_path));
    f.controller->request_mode("file_rvc");
    const std::string expected = fail ? "failed" : "completed";
    f.wait([&] { return f.controller->job_status(finished).value("status", "") == expected; }, "file job did not finish");
    f.controller->request_mode("idle");
    const auto output = f.controller->completed_output(finished);
    if (!fail) require(output.has_value() && fs::exists(*output), "completed output missing");
    const auto pending_path = f.source("pending.wav");
    const auto pending = job_id(f.enqueue(pending_path));
    require(f.controller->clear_finished_jobs().at("count") == 1, "terminal cleanup removed a queued job");
    require(!fs::exists(finished_path), "terminal source not removed");
    if (output) require(!fs::exists(*output), "completed output not removed");
    require(fs::exists(pending_path) && f.controller->job_status(pending).value("status", "") == "queued",
            "queued input did not survive terminal cleanup");
}

void test_disabled() {
    TempDir temporary;
    TestPipeline pipeline(false, false);
    rvc::ModeController::Config config;
    config.rvc_enabled = false;
    config.storage_dir = temporary.path / "jobs";
    config.presets_path = temporary.path / "presets.json";
    rvc::ModeController controller(pipeline, config);
    require(!controller.status()["capabilities"]["rt_rvc"].get<bool>(), "disabled realtime capability advertised");
    require(!controller.status()["capabilities"]["file_rvc"].get<bool>(), "disabled file capability advertised");
    require(controller.request_mode("rt_rvc").value("status", "") == "unavailable", "disabled mode started");
    require(controller.request_mode("idle").value("status", "") == "active", "idle unavailable");
    require(controller.enqueue_file(temporary.path/"missing.wav", "missing.wav", "").value("status", "") == "rejected", "disabled file queue accepted input");
}

void test_file_paths() {
    Fixture f;
    rvc::FileRvcWorker::Config config;
    config.storage_dir = f.temporary.path / "audio";
    const auto executable = f.temporary.path / "ffmpeg '$ tool";
    fs::create_symlink("/usr/bin/ffmpeg", executable);
    config.ffmpeg_path = executable.string();
    rvc::FileRvcWorker worker(f.pipeline, config);
    rvc::FileRvcWorker::Request request{"job_paths", "", f.source("quotes \" '$HOME `name`.wav"),
                                        config.storage_dir / "result '$ audio.wav"};
    std::atomic<bool> cancelled{false};
    std::string error;
    require(worker.process(request, cancelled, [](int) {}, error), "literal paths failed: " + error);
    require(fs::file_size(request.output_path) > 44, "converted WAV missing");
    require(!fs::exists(config.storage_dir / "paths_decoded.f32"), "temporary input retained");
}

void test_file_duration() {
    Fixture f;
    for (const bool rnnoise : {false, true}) {
        rvc::FileRvcWorker::Config config;
        config.storage_dir = f.temporary.path / "audio";
        config.max_duration_seconds = 1;
        config.rnnoise = rnnoise;
        rvc::FileRvcWorker worker(f.pipeline, config);
        rvc::FileRvcWorker::Request request{"job_limit", "", f.source("long.wav", 16001),
                                            config.storage_dir / "result.wav"};
        std::atomic<bool> cancelled{false};
        std::string error;
        require(!worker.process(request, cancelled, [](int) {}, error), "overlong audio was accepted");
        require(error == "audio exceeds 1 second limit", "unexpected error: " + error);
        require(!fs::exists(request.output_path), "overlong result was published");
        require(!fs::exists(config.storage_dir / "limit_decoded.f32"), "decoded input retained");
        require(!fs::exists(config.storage_dir / "limit_decoded_48k.f32"), "48 kHz input retained");
    }
}

void test_ffmpeg_lifecycle(const std::string& scenario) {
    Fixture f;
    const auto marker = f.temporary.path / "child.pid";
    const auto executable = f.temporary.path / "fake ffmpeg";
    {
        std::ofstream script(executable);
        script << "#!/usr/bin/python3\nimport os, sys, time\nfrom pathlib import Path\n"
               << "output = Path(sys.argv[-1])\noutput.write_bytes(bytes(320*4))\n";
        if (scenario == "cancel_encode") script << "if output.suffix != '.part': sys.exit(0)\n";
        script << "Path('" << marker.string() << "').write_text(str(os.getpid()))\n";
        if (scenario == "failure") script << "sys.exit(3)\n";
        else script << "time.sleep(30)\n";
    }
    fs::permissions(executable, fs::perms::owner_all);
    rvc::FileRvcWorker::Config config;
    config.storage_dir = f.temporary.path / "audio";
    config.ffmpeg_path = executable.string();
    config.ffmpeg_timeout_ms = scenario == "timeout" ? 100 : 2000;
    rvc::FileRvcWorker worker(f.pipeline, config);
    rvc::FileRvcWorker::Request request{"job_child", "", f.source("input.wav"), config.storage_dir / "result.wav"};
    std::atomic<bool> cancelled{false};
    std::string error;
    const auto start = std::chrono::steady_clock::now();
    auto result = std::async(std::launch::async, [&] {
        return worker.process(request, cancelled, [](int) {}, error);
    });
    if (scenario == "cancel_decode" || scenario == "cancel_encode") {
        while (!fs::exists(marker) && result.wait_for(5ms) != std::future_status::ready) {
            if (std::chrono::steady_clock::now() - start > 1500ms) break;
        }
        cancelled = true;
    }
    require(!result.get(), "failed or cancelled FFmpeg published success");
    require(std::chrono::steady_clock::now() - start < 1500ms, "FFmpeg cancellation or timeout was too slow");
    const auto expected = scenario == "timeout" ? "ffmpeg deadline exceeded"
        : scenario == "failure" ? "ffmpeg failed" : "cancelled";
    require(error == expected, "unexpected worker error: " + error);
    require(!fs::exists(request.output_path), "incomplete result was published");
    require(!fs::exists(request.output_path.string() + ".part"), "partial output retained");
    require(!fs::exists(config.storage_dir / "child_decoded.f32"), "decoded input retained");
    require(!fs::exists(config.storage_dir / "child_output.f32"), "temporary output retained");
    pid_t child = -1;
    std::ifstream(marker) >> child;
    require(child > 0, "FFmpeg did not start");
    int status = 0;
    require(waitpid(child, &status, WNOHANG) == -1 && errno == ECHILD, "FFmpeg child was not reaped");
}

} // namespace

int main(int argc, char** argv) {
    try {
        require(argc == 2, "expected one test case name");
        const std::string name = argv[1];
        if (name == "file_paths") test_file_paths();
        else if (name == "file_duration") test_file_duration();
        else if (name == "cancel_decode" || name == "cancel_encode" || name == "timeout" || name == "failure") test_ffmpeg_lifecycle(name);
        else if (name == "capacity") test_capacity();
        else if (name == "position") test_position();
        else if (name == "clear_queued") test_clear_queued();
        else if (name == "active_cancel") test_active_cancel();
        else if (name == "eviction") test_eviction();
        else if (name == "clear_completed") test_clear_terminal(false);
        else if (name == "disabled") test_disabled();
        else if (name == "clear_failed") test_clear_terminal(true);
        else throw std::runtime_error("unknown case: " + name);
        std::cout << name << " PASSED\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAILED: " << error.what() << '\n';
        return 1;
    }
}
