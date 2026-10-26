#include "rvc/audio_worker.hpp"
#include "mozart/audio_stream.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <iostream>
#include <thread>

namespace {
using Clock = std::chrono::steady_clock;

class EchoPipeline : public rvc::RVCPipelineBase {
public:
    bool is_mock() const override { return false; }
    bool supports_realtime_streaming() const override { return true; }
    std::vector<float> process(const std::vector<float>& input) override {
        std::vector<float> output;
        for (float value : input) output.insert(output.end(), 3, value);
        return output;
    }
};

// 每帧 PCM 标记其采集序号，独立验证输出源 PTS 和帧确认号。
class PacedStream : public mozart::RealTimeAudioStream {
public:
    explicit PacedStream(int clock_case) : clock_case_(clock_case) {}
    std::atomic<bool> open{true};
    unsigned index = 0, source_index = 0, acknowledgement = 0;
    bool pts_matches = false;
    double observed_ms = 0;
    Clock::time_point start = Clock::now();
    std::vector<Clock::time_point> captured = std::vector<Clock::time_point>(41);

    mozart::StreamDirection GetDirection() const noexcept override { return mozart::StreamDirection::Capture; }
    bool IsOpen() const noexcept override { return open; }
    bool Open(const mozart::StreamConfig&) override { open = true; return true; }
    void Close() override { open = false; }

    bool ReadFrame(void* destination, uint32_t) override {
        if (index >= 40 || !open) { open = false; return false; }
        ++index;
        std::this_thread::sleep_until(start + std::chrono::milliseconds(20 * index));
        auto& frame = *static_cast<mozart_input_frame_t*>(destination);
        frame = {};
        frame.meta.frame_idx = index;
        frame.meta.vad_flag = 1;
        captured[index] = Clock::now();
        frame.meta.pts_ns = source_pts(index);
        std::fill(std::begin(frame.pcm), std::end(frame.pcm), index * .001f);
        return true;
    }

    bool WriteFrame(const void* data, uint32_t) override {
        const auto& frame = *static_cast<const mozart_output_frame_t*>(data);
        const float value = frame.pcm[500];
        const auto source = static_cast<unsigned>(std::lround(value * 1000));
        if (!source_index && source > 0 && source <= index && std::abs(value - source * .001f) < 1e-7f) {
            source_index = source;
            acknowledgement = frame.meta.frame_idx;
            pts_matches = frame.meta.pts_ns == source_pts(source);
            observed_ms = std::chrono::duration<double, std::milli>(Clock::now() - captured[source]).count();
        }
        return clock_case_ != 3;
    }

private:
    int clock_case_;
    uint64_t source_pts(unsigned source) const {
        if (clock_case_ == 2) return 0;
        const auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(captured[source].time_since_epoch()).count();
        return static_cast<uint64_t>(ns) + (clock_case_ == 1 ? 10000000000ull : 0);
    }
};
}

int main() {
    for (int clock_case = 0; clock_case < 4; ++clock_case) {
        EchoPipeline pipeline;
        PacedStream stream(clock_case);
        rvc::AudioWorker::Config config;
        config.realtime_playout_delay_ms = 400;
        rvc::AudioWorker worker(static_cast<mozart::RealTimeAudioStream*>(&stream), pipeline, config);
        worker.start();
        while (worker.running()) std::this_thread::sleep_for(std::chrono::milliseconds(10));
        worker.stop();
        const auto stats = worker.get_latency_stats();
        std::cout << "clock_case=" << clock_case << " source=" << stream.source_index
                  << " acknowledgement=" << stream.acknowledgement
                  << " observed_ms=" << stream.observed_ms << " avg_ms=" << stats.avg_ms << '\n';
        if (!stream.source_index || !stream.pts_matches || stream.acknowledgement <= stream.source_index) return 1;
        if (clock_case == 0 && (stats.count == 0 || stats.avg_ms < 300.0)) return 1;
        if (clock_case != 0 && stats.count != 0) return 1;
    }
    return 0;
}
