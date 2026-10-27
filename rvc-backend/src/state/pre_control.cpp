#include "state/pre_control.hpp"

#include <cerrno>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <fstream>

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

namespace rvc {
namespace {

// 设备标识来自 HTTP 请求。只接受 ALSA 名字的字符集，拒绝空白与 shell 元字符。
constexpr size_t kMaxDeviceIdLength = 128;

bool contains_device(const nlohmann::json& list, const std::string& id)
{
    if (!list.is_array())
        return false;
    for (const auto& item : list) {
        if (item.is_object() && item.value("id", "") == id)
            return true;
    }
    return false;
}

// 脚本会打印人类可读的进度行，JSON 在最后一行。只解析最后一行非空文本。
std::string last_line(const std::string& output)
{
    const size_t end = output.find_last_not_of(" \t\r\n");
    if (end == std::string::npos)
        return {};
    const size_t newline = output.rfind('\n', end);
    const size_t begin = newline == std::string::npos ? 0 : newline + 1;
    return output.substr(begin, end - begin + 1);
}

} // namespace

PreControl::PreControl(Config config) : config_(std::move(config)) {}

bool PreControl::valid_device_id(const std::string& id)
{
    if (id.empty() || id.size() > kMaxDeviceIdLength)
        return false;
    for (const char c : id) {
        const bool allowed = std::isalnum(static_cast<unsigned char>(c)) || c == ':' || c == ',' ||
                             c == '=' || c == '_' || c == '-' || c == '.';
        if (!allowed)
            return false;
    }
    return true;
}

std::vector<std::string> PreControl::restart_argv(const std::filesystem::path& script,
                                                  const std::string& capture,
                                                  const std::string& playback)
{
    return {script.string(), "pre-restart", "--capture", capture, "--playback", playback};
}

nlohmann::json PreControl::read_json_file(const std::filesystem::path& path) const
{
    if (path.empty())
        return nlohmann::json::object();
    std::ifstream input(path);
    if (!input)
        return nlohmann::json::object();
    nlohmann::json parsed = nlohmann::json::parse(input, nullptr, false);
    if (parsed.is_discarded() || !parsed.is_object())
        return nlohmann::json::object();
    return parsed;
}

nlohmann::json PreControl::run_json(const std::vector<std::string>& argv, bool& ok) const
{
    ok = false;
    if (argv.empty())
        return nlohmann::json{{"error", "empty command"}};

    int fds[2];
    if (pipe(fds) != 0)
        return nlohmann::json{{"error", "pipe failed"}};

    const pid_t pid = fork();
    if (pid < 0) {
        close(fds[0]);
        close(fds[1]);
        return nlohmann::json{{"error", "fork failed"}};
    }
    if (pid == 0) {
        close(fds[0]);
        dup2(fds[1], STDOUT_FILENO);
        dup2(fds[1], STDERR_FILENO);
        close(fds[1]);
        // 子进程只应持有标准输入输出。其余 fd（音频端口、监听 socket）
        // 会在 exec 前关闭，避免脚本及其子进程长期占用后端的端口。
        for (int fd = 3; fd < 1024; ++fd)
            close(fd);
        std::vector<char*> args;
        args.reserve(argv.size() + 1);
        for (const auto& item : argv)
            args.push_back(const_cast<char*>(item.c_str()));
        args.push_back(nullptr);
        execv(args[0], args.data());
        _exit(127);
    }

    close(fds[1]);
    std::string output;
    bool timed_out = false;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(config_.timeout_ms);
    for (;;) {
        const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
            deadline - std::chrono::steady_clock::now()).count();
        if (left <= 0) {
            timed_out = true;
            break;
        }
        struct pollfd pfd = { fds[0], POLLIN, 0 };
        const int ready = poll(&pfd, 1, static_cast<int>(left));
        if (ready < 0) {
            if (errno == EINTR)
                continue;
            break;
        }
        if (ready == 0) {
            timed_out = true;
            break;
        }
        char buffer[4096];
        const ssize_t got = read(fds[0], buffer, sizeof(buffer));
        if (got <= 0)
            break;
        output.append(buffer, static_cast<size_t>(got));
        if (output.size() > (1U << 20))
            break;
    }
    close(fds[0]);

    if (timed_out)
        kill(pid, SIGKILL);

    int state = 0;
    while (waitpid(pid, &state, 0) < 0 && errno == EINTR) {
    }

    if (timed_out)
        return nlohmann::json{{"error", "preprocessor restart timed out"}};
    if (!WIFEXITED(state) || WEXITSTATUS(state) != 0) {
        // 脚本把失败原因写在第一行，保留可读片段。
        const auto newline = output.find('\n');
        const std::string reason = output.substr(0, newline == std::string::npos ? 200 : newline);
        return nlohmann::json{{"error", reason.empty() ? "preprocessor script failed" : reason}};
    }

    nlohmann::json parsed = nlohmann::json::parse(last_line(output), nullptr, false);
    if (parsed.is_discarded() || !parsed.is_object())
        return nlohmann::json{{"error", "preprocessor script returned no JSON"}};
    ok = true;
    return parsed;
}

nlohmann::json PreControl::status() const
{
    if (!enabled())
        return nlohmann::json{{"available", false}, {"error", "preprocessor not configured"}};

    nlohmann::json result{{"available", true}};
    bool ok = false;
    auto devices = run_json({config_.script.string(), "devices"}, ok);
    if (!ok)
        result["devices"] = nlohmann::json{{"capture", nlohmann::json::array()},
                                           {"playback", nlohmann::json::array()}};
    else
        result["devices"] = std::move(devices);
    result["current"] = read_json_file(config_.devices_file);
    result["state"] = read_json_file(config_.status_file);
    if (!ok)
        result["error"] = "读取设备清单失败";
    return result;
}

nlohmann::json PreControl::set_devices(const std::string& capture, const std::string& playback)
{
    if (!enabled())
        return nlohmann::json{{"status", "failed"}, {"error", "preprocessor not configured"}};
    if (!valid_device_id(capture) || !valid_device_id(playback))
        return nlohmann::json{{"status", "invalid"}, {"error", "设备标识包含不允许的字符"}};

    bool ok = false;
    auto devices = run_json({config_.script.string(), "devices"}, ok);
    if (!ok)
        return nlohmann::json{{"status", "failed"}, {"error", "读取设备清单失败"}};
    if (!contains_device(devices.value("capture", nlohmann::json::array()), capture))
        return nlohmann::json{{"status", "invalid"}, {"error", "采集设备不存在: " + capture}};
    if (!contains_device(devices.value("playback", nlohmann::json::array()), playback))
        return nlohmann::json{{"status", "invalid"}, {"error", "播放设备不存在: " + playback}};

    auto result = run_json(restart_argv(config_.script, capture, playback), ok);
    if (!ok)
        return nlohmann::json{{"status", "failed"},
                              {"error", result.value("error", std::string("预处理重启失败"))}};
    if (result.value("status", "") != "active")
        return nlohmann::json{{"status", "failed"}, {"error", "预处理未报告 active"}};

    result["current"] = read_json_file(config_.devices_file);
    result["state"] = read_json_file(config_.status_file);
    return result;
}

} // namespace rvc
