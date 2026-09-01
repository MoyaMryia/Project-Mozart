#include "state/file_rvc_worker.hpp"

#include <algorithm>
#include <cstdlib>
#include <fstream>
#include <vector>

#include "mozart.h"

namespace rvc {
namespace {

std::string quote_path(const std::filesystem::path& path) {
    return "\"" + path.string() + "\"";
}

std::vector<float> read_f32le(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary | std::ios::ate);
    if (!input) return {};
    const auto bytes = static_cast<size_t>(input.tellg());
    input.seekg(0);
    std::vector<float> samples(bytes / sizeof(float));
    input.read(reinterpret_cast<char*>(samples.data()),
               static_cast<std::streamsize>(samples.size() * sizeof(float)));
    return samples;
}

} // namespace

FileRvcWorker::FileRvcWorker(RVCPipelineBase& pipeline, Config config)
    : pipeline_(pipeline), config_(std::move(config)) {}

bool FileRvcWorker::process(const Request& request, const std::atomic<bool>& cancelled,
                            const ProgressCallback& progress, std::string& error) {
    const bool switch_model = !request.model_id.empty() && request.model_id != pipeline_.current_model_id();
    if (!pipeline_.is_mock() && switch_model && !pipeline_.switch_model(request.model_id)) {
        error = "requested RVC model was not found or could not be loaded";
        return false;
    }

    const auto raw_input = config_.storage_dir / (request.id.substr(4) + "_decoded.f32");
    const auto raw_output = config_.storage_dir / (request.id.substr(4) + "_output.f32");
    const auto partial_output = request.output_path.string() + ".part";
    std::error_code filesystem_error;

    const auto cleanup = [&]() {
        std::filesystem::remove(raw_input, filesystem_error);
        std::filesystem::remove(raw_output, filesystem_error);
        std::filesystem::remove(partial_output, filesystem_error);
    };

    if (config_.rnnoise) {
        const auto raw_48k = config_.storage_dir / (request.id.substr(4) + "_decoded_48k.f32");
        const std::string decode = config_.ffmpeg_path + " -y -v error -i " + quote_path(request.source_path)
            + " -ac 1 -ar 48000 -f f32le " + quote_path(raw_48k);
        if (std::system(decode.c_str()) != 0) {
            error = "ffmpeg could not decode the uploaded audio";
            std::filesystem::remove(raw_48k, filesystem_error);
            return false;
        }
        const uintmax_t input_bytes = std::filesystem::file_size(raw_48k, filesystem_error);
        std::ifstream input(raw_48k, std::ios::binary);
        std::ofstream output(raw_output, std::ios::binary | std::ios::trunc);
        mozart_dsp_config_t dsp_config{};
        dsp_config.rnnoise = true;
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
        progress(60);
        std::vector<float> converted;
        try {
            converted = pipeline_.process(contract_pcm);
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
        const std::string decode = config_.ffmpeg_path + " -y -v error -i " + quote_path(request.source_path)
            + " -ac 1 -ar 16000 -f f32le " + quote_path(raw_input);
        if (std::system(decode.c_str()) != 0) {
            error = "ffmpeg could not decode the uploaded audio";
            cleanup();
            return false;
        }
        if (cancelled.load()) {
            error = "cancelled";
            cleanup();
            return false;
        }
        progress(20);
        auto audio_16k = read_f32le(raw_input);
        if (audio_16k.empty()) {
            error = "decoded audio was empty";
            cleanup();
            return false;
        }
        progress(40);
        std::vector<float> converted;
        try {
            converted = pipeline_.process(audio_16k);
        } catch (const std::exception& exception) {
            error = std::string("RVC inference failed: ") + exception.what();
            cleanup();
            return false;
        }
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

    const std::string encode = config_.ffmpeg_path + " -y -v error -f f32le -ar "
        + std::to_string(config_.output_sample_rate) + " -ac 1 -i " + quote_path(raw_output)
        + " -f wav -c:a pcm_f32le " + quote_path(partial_output);
    const bool succeeded = std::system(encode.c_str()) == 0;
    if (succeeded) std::filesystem::rename(partial_output, request.output_path, filesystem_error);
    if (!succeeded || filesystem_error) {
        error = "ffmpeg could not encode the converted audio";
        cleanup();
        return false;
    }
    progress(100);
    cleanup();
    return true;
}

} // namespace rvc
