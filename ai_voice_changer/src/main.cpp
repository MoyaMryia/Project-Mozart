// src/main.cpp
#include "contract/contract_source.hpp"
#include "contract/mock_source.hpp"
#include "contract/mozart_ring_source.hpp"
#include "inference/infer_engine.hpp"
#include "output/dummy_sink.hpp"
#include "rvc/model_loader.hpp"
#include "rvc/rvc_pipeline.hpp"
#include "server/http_server.hpp"
#include "utils/config.hpp"
#include "utils/logging.hpp"
#include "frame_meta.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <memory>
#include <span>
#include <thread>
#include <vector>

#ifdef MOZART_ENABLE_PIPEWIRE
#include "output/pipewire_sink.hpp"
#endif

namespace {

std::atomic<bool> g_running{true};

void on_signal(int) { g_running = false; }

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT,  on_signal);
    std::signal(SIGTERM, on_signal);

    mozart::AppConfig cfg = mozart::load_config(
        (argc > 1) ? std::filesystem::path(argv[1]) : "config.toml");

    // ---- Contract source ------------------------------------------------
    std::unique_ptr<mozart::ContractSource> source;
    if (cfg.contract.source == "mock") {
        source = std::make_unique<mozart::MockSource>(440.0f,
                                                       cfg.audio.input_rate);
    } else if (cfg.contract.source == "mozart") {
        try {
            source = std::make_unique<mozart::MozartRingSource>(
                cfg.contract.ring_name);
        } catch (const std::exception& e) {
            MOZART_ERROR("Contract source mozart init failed: %s", e.what());
            return 1;
        }
    } else {
        MOZART_ERROR("Unknown contract.source '%s'", cfg.contract.source.c_str());
        return 1;
    }

    // ---- RVC pipeline --------------------------------------------------
    std::unique_ptr<mozart::RVCPipeline> pipeline;
    try {
        pipeline = mozart::make_pipeline(
            cfg.rvc.mock_mode, cfg.rvc.assets_dir, cfg.rvc.hubert_path,
            cfg.rvc.rmvpe_path, cfg.rvc.models_dir, cfg.rvc.device,
            cfg.rvc.index_rate);
    } catch (const std::exception& e) {
        MOZART_ERROR("Pipeline init failed: %s", e.what());
        return 1;
    }

    // ---- Output sink ----------------------------------------------------
    std::unique_ptr<mozart::OutputSink> sink;
    if (cfg.output.sink == "dummy") {
        sink = std::make_unique<mozart::DummySink>();
#ifdef MOZART_ENABLE_PIPEWIRE
    } else if (cfg.output.sink == "pipewire") {
        sink = std::make_unique<mozart::PipeWireSink>(cfg.output.source_name,
                                                       cfg.audio.output_rate,
                                                       cfg.audio.channels);
#endif
    } else {
        MOZART_ERROR("Unknown output.sink '%s'", cfg.output.sink.c_str());
        return 1;
    }

    // ---- HTTP server ----------------------------------------------------
    mozart::ModelManager models(cfg.rvc.models_dir);
    mozart::PipelineStats stats;
    mozart::HttpServer http(cfg.server.host, cfg.server.port, models, stats,
                            cfg.rvc.mock_mode ? "mock" : "real");
    http.wait_ready();

    MOZART_INFO("main loop up: source=%s pipeline=%s sink=%s mode=%s",
                source->name(), pipeline->name(), sink->name(),
                cfg.rvc.mock_mode ? "mock" : "real");

    mozart::ContractFrame in_pcm{};
    mozart::FrameMeta     meta{};
    std::vector<float>    out_buf(mozart::kOutputSamples);
    uint8_t               last_segment_id = 0;
    bool                  last_segment_set = false;

    while (g_running) {
        using clock = std::chrono::steady_clock;
        auto t0 = clock::now();

        auto rc = source->poll(in_pcm, meta, cfg.contract.poll_timeout_us);
        if (rc == mozart::PollResult::Timeout) continue;
        if (rc == mozart::PollResult::Error) {
            MOZART_ERROR("Contract source error; stopping");
            break;
        }

        // segment boundary -> reset streaming state
        if (last_segment_set && meta.segment_id != last_segment_id) {
            pipeline->reset_state();
        }
        last_segment_id = meta.segment_id;
        last_segment_set = true;

        std::span<float> out(out_buf);
        if (meta.vad_flag == 0) {
            std::fill(out.begin(), out.end(), 0.0f);
            stats.frames_silence.fetch_add(1, std::memory_order_relaxed);
        } else {
            pipeline->run(std::span<const float>(in_pcm), meta, out);
            stats.frames_processed.fetch_add(1, std::memory_order_relaxed);
        }
        sink->write(out);

        auto t1 = clock::now();
        double ms =
            std::chrono::duration<double, std::milli>(t1 - t0).count();
        stats.last_latency_ms.store(ms, std::memory_order_relaxed);
        // crude rolling average
        double avg = stats.avg_latency_ms.load(std::memory_order_relaxed);
        avg = (avg == 0.0) ? ms : (avg * 0.95 + ms * 0.05);
        stats.avg_latency_ms.store(avg, std::memory_order_relaxed);

        if ((stats.frames_processed.load() + stats.frames_silence.load()) %
                100 == 0) {
            MOZART_INFO("latency last=%.2fms avg=%.2fms processed=%llu silence=%llu",
                        ms, avg,
                        static_cast<unsigned long long>(
                            stats.frames_processed.load()),
                        static_cast<unsigned long long>(
                            stats.frames_silence.load()));
        }
    }

    MOZART_INFO("shutdown");
    return 0;
}