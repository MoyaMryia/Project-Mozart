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

    const auto raw_input = config_.storage_dir / (request.id.substr(4) + "_decoded_48k.f32");
    const auto raw_output = config_.storage_dir / (request.id.substr(4) + "_output.f32");
    const auto partial_output = request.output_path.string() + ".part";
    const std::string decode = config_.ffmpeg_path + " -y -v error -i " + quote_path(request.source_path)
        + " -ac 1 -ar 48000 -f f32le " + quote_path(raw_input);
    std::error_code filesystem_error;
    if (std::system(decode.c_str()) != 0) {
        error = "ffmpeg could not decode the uploaded audio";
        return false;
    }

    const uintmax_t input_bytes = std::filesystem::file_size(raw_input, filesystem_error);
    std::ifstream input(raw_input, std::ios::binary);
    std::ofstream output(raw_output, std::ios::binary | std::ios::trunc);
    mozart_dsp_config_t dsp_config{};
    dsp_config.rnnoise = true;
    mozart_dsp_t* preprocessor = mozart_dsp_new(&dsp_config);
    if (!input || !output || !preprocessor) {
        error = "failed to initialize offline preprocessing";
        std::filesystem::remove(raw_input, filesystem_error);
        return false;
    }

    std::vector<float> raw_frame(MOZART_RAW_SAMPLES, 0.0f);
    // The deployed generator emits 96,000 samples for T=200: exactly two
    // seconds at 48 kHz. Keep input chunks at two seconds so each generated
    // block preserves source duration; inferencer resamples features to T=200.
    constexpr size_t contract_chunk_samples = MOZART_INPUT_SAMPLES * 100;
    std::vector<float> contract_chunk;
    contract_chunk.reserve(contract_chunk_samples);
    std::vector<bool> voiced_frames;
    voiced_frames.reserve(100);
    uintmax_t processed_bytes = 0;
    bool succeeded = true;
    const auto process_chunk = [&](size_t source_output_samples) {
        if (contract_chunk.empty() || !succeeded) return;
        contract_chunk.resize(contract_chunk_samples, 0.0f);
        voiced_frames.resize(100, false);
        std::vector<float> converted;
        try {
            converted = pipeline_.process(contract_chunk);
        } catch (const std::exception& exception) {
            error = std::string("RVC inference failed: ") + exception.what();
            succeeded = false;
            return;
        }
        if (converted.empty()) {
            error = "RVC inference returned no audio";
            succeeded = false;
            return;
        }
        const size_t output_samples = converted.size() * source_output_samples
            / (MOZART_RAW_SAMPLES * 100);
        // Offline conversion must not feed generated noise back into detected
        // silence. Keep one 20 ms frame on either side of speech for consonants.
        std::vector<bool> audible = voiced_frames;
        for (size_t frame = 0; frame < voiced_frames.size(); ++frame) {
            if (!voiced_frames[frame]) continue;
            if (frame > 0) audible[frame - 1] = true;
            if (frame + 1 < audible.size()) audible[frame + 1] = true;
        }
        constexpr size_t fade_samples = 240; // 5 ms at the fixed 48 kHz output rate.
        float gain = audible.empty() || !audible.front() ? 0.0f : 1.0f;
        for (size_t sample = 0; sample < output_samples; ++sample) {
            const size_t frame = std::min(audible.size() - 1, sample * audible.size() / converted.size());
            const float target = audible[frame] ? 1.0f : 0.0f;
            gain += (target - gain) / std::min(fade_samples, output_samples - sample);
            converted[sample] *= gain;
        }
        output.write(reinterpret_cast<const char*>(converted.data()),
                     static_cast<std::streamsize>(output_samples * sizeof(float)));
        if (!output) {
            error = "failed to write processed audio";
            succeeded = false;
        }
        contract_chunk.clear();
        voiced_frames.clear();
    };
    size_t source_output_samples = 0;
    while (true) {
        input.read(reinterpret_cast<char*>(raw_frame.data()), static_cast<std::streamsize>(raw_frame.size() * sizeof(float)));
        const std::streamsize bytes_read = input.gcount();
        if (bytes_read <= 0) break;
        if (cancelled.load()) {
            error = "cancelled";
            succeeded = false;
            break;
        }
        const size_t samples_read = static_cast<size_t>(bytes_read) / sizeof(float);
        std::fill(raw_frame.begin() + static_cast<std::ptrdiff_t>(samples_read), raw_frame.end(), 0.0f);
        // New preprocessor contract: 48 kHz interleaved S16 stereo in.
        // Offline input is f32 mono — clamp, quantize, duplicate to both channels.
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
        contract_chunk.insert(contract_chunk.end(), contract.pcm, contract.pcm + MOZART_INPUT_SAMPLES);
        voiced_frames.push_back(contract.meta.vad_flag != 0);
        source_output_samples += samples_read;
        processed_bytes += static_cast<uintmax_t>(bytes_read);
        if (input_bytes > 0) progress(std::min(99, static_cast<int>(processed_bytes * 100 / input_bytes)));
        if (contract_chunk.size() == contract_chunk_samples) {
            process_chunk(source_output_samples);
            source_output_samples = 0;
        }
        if (!succeeded) break;
    }
    if (succeeded && !cancelled.load()) process_chunk(source_output_samples);
    mozart_dsp_free(preprocessor);
    input.close();
    output.close();

    if (succeeded && !cancelled.load()) {
        const std::string encode = config_.ffmpeg_path + " -y -v error -f f32le -ar "
            + std::to_string(config_.output_sample_rate) + " -ac 1 -i " + quote_path(raw_output)
            + " -f wav -c:a pcm_f32le " + quote_path(partial_output);
        succeeded = std::system(encode.c_str()) == 0;
        if (succeeded) std::filesystem::rename(partial_output, request.output_path, filesystem_error);
        if (!succeeded || filesystem_error) error = "ffmpeg could not encode the converted audio";
    } else if (cancelled.load()) {
        error = "cancelled";
    }

    std::filesystem::remove(raw_input, filesystem_error);
    std::filesystem::remove(raw_output, filesystem_error);
    std::filesystem::remove(partial_output, filesystem_error);
    return succeeded && !cancelled.load();
}

} // namespace rvc
