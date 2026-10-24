#include "state/file_rvc_worker.hpp"

#include <algorithm>
#include <cstdlib>
#include <fstream>
#include <vector>
#include <chrono>
#include <thread>
#include <cerrno>
#include <csignal>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>

extern char** environ;

#include "mozart.h"

namespace rvc {
namespace {

bool run_ffmpeg(const std::vector<std::string>& arguments, const std::atomic<bool>& cancelled,
                uint32_t timeout_ms, std::string& error) {
    if (cancelled.load()) { error = "cancelled"; return false; }
    std::vector<char*> argv;
    for (const auto& argument : arguments) argv.push_back(const_cast<char*>(argument.c_str()));
    argv.push_back(nullptr);
    posix_spawnattr_t attributes;
    if (posix_spawnattr_init(&attributes) != 0) { error = "failed to initialize ffmpeg process"; return false; }
    posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETPGROUP);
    posix_spawnattr_setpgroup(&attributes, 0);
    pid_t pid = -1;
    const int launched = posix_spawnp(&pid, argv[0], nullptr, &attributes, argv.data(), environ);
    posix_spawnattr_destroy(&attributes);
    if (launched != 0) { error = "failed to start ffmpeg"; return false; }
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    while (true) {
        int status = 0;
        const auto result = waitpid(pid, &status, WNOHANG);
        if (result == pid) {
            if (cancelled.load()) { error = "cancelled"; return false; }
            if (WIFEXITED(status) && WEXITSTATUS(status) == 0) return true;
            error = "ffmpeg failed";
            return false;
        }
        if (result < 0 && errno != EINTR) { error = "failed to wait for ffmpeg"; return false; }
        if (cancelled.load() || std::chrono::steady_clock::now() >= deadline) {
            error = cancelled.load() ? "cancelled" : "ffmpeg deadline exceeded";
            kill(-pid, SIGKILL);
            while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
            return false;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
}

std::vector<float> read_f32le(const std::filesystem::path& path, uintmax_t max_bytes) {
    std::ifstream input(path, std::ios::binary | std::ios::ate);
    if (!input) return {};
    const auto end_position = input.tellg();
    if (end_position <= 0) return {};
    const auto bytes = static_cast<size_t>(end_position);
    if (bytes > max_bytes || bytes % sizeof(float) != 0) return {};
    input.seekg(0);
    std::vector<float> samples(bytes / sizeof(float));
    input.read(reinterpret_cast<char*>(samples.data()),
               static_cast<std::streamsize>(samples.size() * sizeof(float)));
    return input ? samples : std::vector<float>{};
}

} // namespace

FileRvcWorker::FileRvcWorker(RVCPipelineBase& pipeline, Config config)
    : pipeline_(pipeline), config_(std::move(config)) {}

bool FileRvcWorker::process(const Request& request, const std::atomic<bool>& cancelled,
                            const ProgressCallback& progress, std::string& error) {
    if (cancelled.load()) { error = "cancelled"; return false; }
    if (config_.max_duration_seconds == 0 || config_.ffmpeg_timeout_ms == 0) {
        error = "invalid file processing limits";
        return false;
    }
    const bool switch_model = !request.model_id.empty() && request.model_id != pipeline_.current_model_id();
    if (!pipeline_.is_mock() && switch_model && !pipeline_.switch_model(request.model_id)) {
        error = "requested RVC model was not found or could not be loaded";
        return false;
    }

    const auto raw_input = config_.storage_dir / (request.id.substr(4) + "_decoded.f32");
    const auto raw_48k = config_.storage_dir / (request.id.substr(4) + "_decoded_48k.f32");
    const auto raw_output = config_.storage_dir / (request.id.substr(4) + "_output.f32");
    const auto partial_output = request.output_path.string() + ".part";
    std::error_code filesystem_error;

    const auto cleanup = [&]() {
        std::filesystem::remove(raw_input, filesystem_error);
        std::filesystem::remove(raw_48k, filesystem_error);
        std::filesystem::remove(raw_output, filesystem_error);
        std::filesystem::remove(partial_output, filesystem_error);
    };

    // 退出路径统一删除临时文件，包括推理异常和取消。
    struct Cleanup {
        const decltype(cleanup)& run;
        ~Cleanup() { run(); }
    } guard{cleanup};
    const auto decode = [&](const std::filesystem::path& output, uint32_t sample_rate) {
        const uintmax_t max_bytes = uintmax_t(config_.max_duration_seconds) * sample_rate * sizeof(float);
        const std::vector<std::string> arguments{
            config_.ffmpeg_path, "-nostdin", "-y", "-v", "error", "-i", request.source_path.string(),
            "-t", std::to_string(uint64_t(config_.max_duration_seconds) + 1),
            "-fs", std::to_string(max_bytes + sizeof(float)),
            "-ac", "1", "-ar", std::to_string(sample_rate), "-f", "f32le", output.string()
        };
        if (!run_ffmpeg(arguments, cancelled, config_.ffmpeg_timeout_ms, error)) return false;
        std::error_code size_error;
        const auto bytes = std::filesystem::file_size(output, size_error);
        if (size_error || bytes == 0 || bytes % sizeof(float) != 0) {
            error = "decoded audio was empty or invalid";
            return false;
        }
        if (bytes > max_bytes) {
            error = "audio exceeds " + std::to_string(config_.max_duration_seconds) + " second limit";
            return false;
        }
        return true;
    };
    if (config_.denoise) {
        if (!decode(raw_48k, 48000)) return false;
        const uintmax_t input_bytes = std::filesystem::file_size(raw_48k, filesystem_error);
        std::ifstream input(raw_48k, std::ios::binary);
        std::ofstream output(raw_output, std::ios::binary | std::ios::trunc);
        mozart_dsp_config_t dsp_config{};
        dsp_config.denoise = true;
        mozart_dsp_t* preprocessor = mozart_dsp_new(&dsp_config);
        if (!input || !output || !preprocessor) {
            error = "failed to initialize offline preprocessing";
            if (preprocessor) mozart_dsp_free(preprocessor);
            std::filesystem::remove(raw_48k, filesystem_error);
            std::filesystem::remove(raw_output, filesystem_error);
            return false;
        }

        std::vector<float> raw_frame(MOZART_RAW_SAMPLES, 0.0f);
        std::vector<float> contract_pcm;
        size_t valid_contract_samples = 0;
        uintmax_t processed_bytes = 0;
        bool succeeded = true;
        while (true) {
            input.read(reinterpret_cast<char*>(raw_frame.data()),
                       static_cast<std::streamsize>(raw_frame.size() * sizeof(float)));
            const std::streamsize bytes_read = input.gcount();
            if (bytes_read <= 0) break;
            if (cancelled.load()) {
                error = "cancelled";
                succeeded = false;
                break;
            }
            const size_t samples_read = static_cast<size_t>(bytes_read) / sizeof(float);
            std::fill(raw_frame.begin() + static_cast<std::ptrdiff_t>(samples_read), raw_frame.end(), 0.0f);
            int16_t stereo_frame[MOZART_RAW_SAMPLES * 2];
            for (size_t i = 0; i < MOZART_RAW_SAMPLES; ++i) {
                float s = raw_frame[i];
                if (s > 1.0f) s = 1.0f;
                if (s < -1.0f) s = -1.0f;
                const auto v = static_cast<int16_t>(s * 32767.0f);
                stereo_frame[2 * i] = v;
                stereo_frame[2 * i + 1] = v;
            }
            mozart_input_frame_t contract{};
            if (mozart_dsp_process(preprocessor, stereo_frame, &contract) != 0) {
                error = "preprocessor failed";
                succeeded = false;
                break;
            }
            if (contract.meta.vad_flag == 0) {
                std::fill(std::begin(contract.pcm), std::end(contract.pcm), 0.0f);
            }
            contract_pcm.insert(contract_pcm.end(), contract.pcm, contract.pcm + MOZART_INPUT_SAMPLES);
            valid_contract_samples += (samples_read + 2) / 3;
            processed_bytes += static_cast<uintmax_t>(bytes_read);
            if (input_bytes > 0) progress(std::min(50, static_cast<int>(processed_bytes * 50 / input_bytes)));
        }
        mozart_dsp_free(preprocessor);
        input.close();
        std::filesystem::remove(raw_48k, filesystem_error);
        if (!succeeded) {
            cleanup();
            return false;
        }
        if (cancelled.load()) { error = "cancelled"; return false; }
        progress(60);
        std::vector<float> converted;
        try {
            converted = pipeline_.process_cancellable(contract_pcm, cancelled);
        } catch (const std::exception& exception) {
            error = std::string("RVC inference failed: ") + exception.what();
            cleanup();
            return false;
        }
        const size_t valid_output_samples = valid_contract_samples * config_.output_sample_rate / 16000;
        if (converted.size() > valid_output_samples) converted.resize(valid_output_samples);
        if (converted.empty()) {
            error = "RVC inference returned no audio";
            cleanup();
            return false;
        }
        output.write(reinterpret_cast<const char*>(converted.data()),
                     static_cast<std::streamsize>(converted.size() * sizeof(float)));
        const bool wrote = static_cast<bool>(output);
        output.close();
        if (!wrote) {
            error = "failed to write processed audio";
            cleanup();
            return false;
        }
    } else {
        if (!decode(raw_input, 16000)) return false;
        if (cancelled.load()) {
            error = "cancelled";
            cleanup();
            return false;
        }
        progress(20);
        auto audio_16k = read_f32le(raw_input, uintmax_t(config_.max_duration_seconds) * 16000 * sizeof(float));
        if (audio_16k.empty()) {
            error = "decoded audio was empty";
            cleanup();
            return false;
        }
        progress(40);
        std::vector<float> converted;
        try {
            converted = pipeline_.process_cancellable(audio_16k, cancelled);
        } catch (const std::exception& exception) {
            error = std::string("RVC inference failed: ") + exception.what();
            cleanup();
            return false;
        }
        // 与 RNNoise 分支一致：裁到输入时长对应的输出样本数，避免块边界漂移。
        const size_t expected_output = audio_16k.size() * config_.output_sample_rate / 16000;
        if (converted.size() > expected_output) converted.resize(expected_output);
        if (converted.empty()) {
            error = "RVC inference returned no audio";
            cleanup();
            return false;
        }
        progress(85);
        std::ofstream output(raw_output, std::ios::binary | std::ios::trunc);
        output.write(reinterpret_cast<const char*>(converted.data()),
                     static_cast<std::streamsize>(converted.size() * sizeof(float)));
        const bool wrote = static_cast<bool>(output);
        output.close();
        if (!wrote) {
            error = "failed to write processed audio";
            cleanup();
            return false;
        }
    }

    if (cancelled.load()) {
        error = "cancelled";
        cleanup();
        return false;
    }

    const std::vector<std::string> encode{
        config_.ffmpeg_path, "-nostdin", "-y", "-v", "error", "-f", "f32le", "-ar",
        std::to_string(config_.output_sample_rate), "-ac", "1", "-i", raw_output.string(),
        "-f", "wav", "-c:a", "pcm_f32le", partial_output
    };
    if (!run_ffmpeg(encode, cancelled, config_.ffmpeg_timeout_ms, error)) return false;
    if (cancelled.load()) { error = "cancelled"; return false; }
    std::filesystem::rename(partial_output, request.output_path, filesystem_error);
    if (filesystem_error) {
        error = "failed to publish converted audio";
        return false;
    }
    progress(100);
    cleanup();
    return true;
}

} // namespace rvc
