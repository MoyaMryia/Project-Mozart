#include <mozart/utils/config.hpp>
#include <mozart/utils/logging.hpp>
#include <mozart/source/source_base.hpp>
#include <mozart/source/mock_source.hpp>
#include <mozart/source/ring_source.hpp>
#include <mozart/source/udp_source.hpp>
#include <mozart/rvc/rvc_pipeline.hpp>
#include <mozart/output/sink_base.hpp>
#include <mozart/output/dummy_sink.hpp>
#include <mozart/output/pipewire_sink.hpp>
#include <mozart/output/udp_sink.hpp>
#include <mozart/server/http_server.hpp>
#include <csignal>
#include <atomic>

using namespace mozart;

static std::atomic<bool> g_running{true};

static void signal_handler(int) {
    g_running = false;
}

int main(int argc, char* argv[]) {
    // Initialize logging
    init_logging("info");

    // Load config
    std::string config_path = (argc > 1) ? argv[1] : "config.toml";
    Config cfg = Config::load(config_path);

    MOZART_LOG_INFO("Mozart Unified v0.2.0 starting");
    MOZART_LOG_INFO("Config: {}", config_path);

    // Create source
    std::unique_ptr<SourceBase> source;
    if (cfg.source.type == "mock") {
        source = std::make_unique<MockSource>();
    } else if (cfg.source.type == "ring") {
        source = std::make_unique<RingSource>(cfg.source.ring_name);
    } else if (cfg.source.type == "udp") {
        source = std::make_unique<UdpSource>(cfg.source.udp);
    } else {
        MOZART_LOG_ERROR("Unknown source type: {}", cfg.source.type);
        return 1;
    }
    MOZART_LOG_INFO("Source: {}", source->name());

    // Create RVC pipeline
    RealRVCPipeline::Config rvc_cfg;
    rvc_cfg.model_dir = cfg.rvc.models_dir;
    rvc_cfg.hubert_model = cfg.rvc.hubert_model;
    rvc_cfg.params = cfg.rvc.params;

    auto pipeline = create_pipeline(cfg.rvc.mock_mode, rvc_cfg);
    MOZART_LOG_INFO("Pipeline: {}", pipeline->name());

    // Create output sink
    std::unique_ptr<SinkBase> sink;
    if (cfg.output.type == "dummy") {
        sink = std::make_unique<DummySink>();
    } else if (cfg.output.type == "pipewire") {
        sink = std::make_unique<PipeWireSink>(cfg.output.pipewire);
    } else if (cfg.output.type == "udp") {
        sink = std::make_unique<UdpSink>(cfg.output.udp);
    } else {
        MOZART_LOG_ERROR("Unknown output type: {}", cfg.output.type);
        return 1;
    }
    MOZART_LOG_INFO("Output: {}", sink->name());

    // Start HTTP server
    std::unique_ptr<HttpServer> http;
    if (cfg.server.enabled) {
        HttpServer::Callbacks cbs;
        cbs.on_health = []() { return R"({"status":"ok"})"; };
        cbs.on_status = [&]() {
            return R"({"source":")" + std::string(source->name()) +
                   R"(","pipeline":")" + std::string(pipeline->name()) +
                   R"(","sink":")" + std::string(sink->name()) + R"("})";
        };
        http = std::make_unique<HttpServer>(cfg.server.config, cbs);
        http->start();
    }

    // Signal handling
    std::signal(SIGINT, signal_handler);
    std::signal(SIGTERM, signal_handler);

    // Main loop
    MOZART_LOG_INFO("Entering main loop");
    FrameBuf in_frame;
    OutputFrameBuf out_frame;
    uint8_t last_segment_id = 0;

    while (g_running) {
        if (!source->poll(in_frame, 100000)) {
            continue;  // Timeout
        }

        // VAD short-circuit: skip inference for silence
        if (in_frame.meta.vad_flag == 0) {
            out_frame.samples.fill(0.0f);
        } else {
            // Segment change: reset pipeline state
            if (in_frame.meta.segment_id != last_segment_id) {
                pipeline->reset_state();
                last_segment_id = in_frame.meta.segment_id;
            }
            pipeline->process(in_frame, out_frame);
        }

        sink->write(out_frame);
    }

    MOZART_LOG_INFO("Shutting down");
    if (http) http->stop();

    return 0;
}
