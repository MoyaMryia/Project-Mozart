#include "mozart/control_api.hpp"
#include "state/mode_controller.hpp"
#include <cstdlib>
#include <filesystem>
#include <iostream>

int main(int argc, char** argv) {
    if (argc != 2) return 2;
    char pattern[] = "/tmp/mozart-control-fixture-XXXXXX";
    const char* directory = mkdtemp(pattern);
    if (!directory) return 1;
    const std::filesystem::path root(directory);
    int result = 0;
    {
        rvc::MockRVCPipeline pipeline;
        rvc::ModeController::Config config;
        config.audio_host = "127.0.0.1";
        config.audio_port = 0;
        config.storage_dir = root / "jobs";
        config.presets_path = root / "presets.json";
        rvc::ModeController controller(pipeline, config);
        rvc::ControlApiServer server(argv[1], &controller);
        if (!server.start()) result = 1;
        else std::cin.get();
        server.stop();
    }
    std::filesystem::remove_all(root);
    return result;
}
