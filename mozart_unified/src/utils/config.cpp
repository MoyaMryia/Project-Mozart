#include <mozart/utils/config.hpp>
#include <mozart/utils/logging.hpp>
#include <fstream>
#include <stdexcept>

// toml++ is header-only, vendored in third_party/
// For now, use a simple stub implementation
// TODO: Integrate toml++ when vendored

namespace mozart {

Config Config::load(const std::string& path) {
    Config cfg;

    // TODO: Implement actual TOML parsing with toml++
    // For now, just return defaults

    std::ifstream f(path);
    if (!f.is_open()) {
        MOZART_LOG_WARN("Config file not found: {}, using defaults", path);
        return cfg;
    }

    MOZART_LOG_INFO("Loaded config from {}", path);
    return cfg;
}

}  // namespace mozart
