#pragma once

#include <chrono>
#include <filesystem>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace rvc {

// 预处理进程由外部脚本管理。控制面只读设备清单、读当前状态、请求切换设备。
// 进程生命周期留在脚本里，这里不发信号、不持有子进程句柄。
class PreControl {
public:
    struct Config {
        std::filesystem::path script;        // run-realtime-voice.sh
        std::filesystem::path status_file;   // pre-status.json
        std::filesystem::path devices_file;  // pre-devices.json
        int timeout_ms = 20000;
    };

    explicit PreControl(Config config);

    bool enabled() const { return !config_.script.empty(); }

    // {available, devices:{capture,playback}, current:{capture,playback}, state:{...}}
    nlohmann::json status() const;

    // 设备必须在脚本给出的清单里，然后用脚本重启预处理。
    nlohmann::json set_devices(const std::string& capture, const std::string& playback);

    // 纯函数：设备标识只允许 ALSA 名字用到的字符。
    static bool valid_device_id(const std::string& id);

    // 纯函数：命令参数按固定顺序排列，不经过 shell。
    static std::vector<std::string> restart_argv(const std::filesystem::path& script,
                                                 const std::string& capture,
                                                 const std::string& playback);

private:
    nlohmann::json run_json(const std::vector<std::string>& argv, bool& ok) const;
    nlohmann::json read_json_file(const std::filesystem::path& path) const;

    Config config_;
};

} // namespace rvc
