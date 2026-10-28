// control_api.hpp — 二进制控制协议服务（M4，Unix 数据报套接字）
// ============================================================================
// 与 HTTP API 并存（迁移第一步）：同一 ControlPlane 表面的协议化子集。
// 消息契约见 IO/include/mozart/ctlmsg.h；本服务只实现 PING/STATUS/MODE_SET，
// 其余命令按 ERR_UNKNOWN 应答并保持服务在线。
#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <vector>
#include <memory>
#include <string>
#include <thread>

#include "state/control_plane.hpp"

namespace rvc {

// 解析一条控制数据报；传输层负责接收和发送。
std::vector<unsigned char> control_reply(ControlPlane& controller, const unsigned char* data, size_t size);

class ControlApiServer {
public:
    ControlApiServer(const std::string& socket_path, ControlPlane* controller);
    ~ControlApiServer();

    bool start();
    void stop();

    uint64_t requests() const { return requests_.load(); }

private:
    void serve();

    std::string socket_path_;
    ControlPlane* controller_;
    std::atomic<bool> running_{false};
    std::thread server_thread_;
    int server_fd_ = -1;
    std::atomic<uint64_t> requests_{0};
};

} // namespace rvc
