#include "mozart/monitor.hpp"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <optional>
#include <string>
#include <sys/types.h>
#include <unistd.h>

namespace rvc {
namespace {

uint64_t read_meminfo_kib(const std::string& key) {
    std::ifstream input("/proc/meminfo");
    std::string name;
    uint64_t value = 0;
    std::string unit;
    while (input >> name >> value >> unit) {
        if (name == key + ":") return value;
    }
    return 0;
}

bool path_exists(const std::string& path) {
    std::error_code error;
    return std::filesystem::exists(path, error);
}

std::optional<uint64_t> read_uint(const std::string& path) {
    std::ifstream input(path);
    uint64_t value = 0;
    return input >> value ? std::optional<uint64_t>(value) : std::nullopt;
}

} // namespace

nlohmann::json SystemMonitor::snapshot() {
    nlohmann::json result;

    std::ifstream cpu_file("/proc/stat");
    std::string label;
    uint64_t user = 0;
    uint64_t nice = 0;
    uint64_t system = 0;
    uint64_t idle = 0;
    uint64_t iowait = 0;
    uint64_t irq = 0;
    uint64_t softirq = 0;
    uint64_t steal = 0;
    cpu_file >> label >> user >> nice >> system >> idle >> iowait >> irq >> softirq >> steal;
    const uint64_t total = user + nice + system + idle + iowait + irq + softirq + steal;
    const uint64_t idle_total = idle + iowait;
    if (previous_cpu_total_ > 0 && total > previous_cpu_total_) {
        const uint64_t total_delta = total - previous_cpu_total_;
        const uint64_t idle_delta = idle_total - previous_cpu_idle_;
        result["cpu_percent"] = 100.0 * (1.0 - static_cast<double>(idle_delta) / total_delta);
    } else {
        result["cpu_percent"] = nullptr;
    }
    previous_cpu_total_ = total;
    previous_cpu_idle_ = idle_total;

    const uint64_t memory_total = read_meminfo_kib("MemTotal") * 1024;
    const uint64_t memory_available = read_meminfo_kib("MemAvailable") * 1024;
    result["memory"] = {
        {"total_bytes", memory_total},
        {"used_bytes", memory_total > memory_available ? memory_total - memory_available : 0}
    };

    const char* runtime_dir = std::getenv("XDG_RUNTIME_DIR");
    const std::string pipewire_socket = runtime_dir
        ? std::string(runtime_dir) + "/pipewire-0"
        : "/run/user/" + std::to_string(getuid()) + "/pipewire-0";
    result["pipewire"] = {{"available", path_exists(pipewire_socket)}};

    const bool cuda_available = path_exists("/dev/nvidia0") || path_exists("/dev/nvhost-gpu");
    result["cuda"] = {{"available", cuda_available}};

    const auto gpu_load = read_uint("/sys/devices/platform/bus@0/17000000.gpu/load");
    // Jetson has unified memory, so RAM is the only truthful GPU-memory pool.
    result["gpu"] = {{"available", gpu_load.has_value()}, {"load_percent", gpu_load ? *gpu_load / 10.0 : 0.0},
                     {"memory_type", "shared"}, {"memory_total_bytes", memory_total},
                     {"memory_used_bytes", memory_total > memory_available ? memory_total - memory_available : 0}};
    return result;
}

} // namespace rvc
