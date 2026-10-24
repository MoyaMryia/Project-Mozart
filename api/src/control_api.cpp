// control_api.cpp — 二进制控制协议服务实现
// ============================================================================
// Unix 数据报套接字：一请求一应答，天然免流式分帧。模式字符串与
// ControlPlane::request_mode 的映射：0=idle 1=realtime 2=file（默认模型）。
#include "mozart/control_api.hpp"
#include "mozart/ctlmsg.h"

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <array>
#include <cstring>
#include <nlohmann/json.hpp>
#include <spdlog/spdlog.h>

namespace rvc {
namespace {

constexpr size_t kMaxDatagram = 4096;

void put_u16(unsigned char* p, uint16_t v) { std::memcpy(p, &v, 2); }
void put_u32(unsigned char* p, uint32_t v) { std::memcpy(p, &v, 4); }
uint16_t get_u16(const unsigned char* p) { uint16_t v; std::memcpy(&v, p, 2); return v; }
uint32_t get_u32(const unsigned char* p) { uint32_t v; std::memcpy(&v, p, 4); return v; }

std::vector<unsigned char> make_error(uint32_t req_id, uint32_t code, const char* text)
{
    const size_t text_len = std::strlen(text);
    std::vector<unsigned char> out(8 + 4 + 4 + text_len);
    out[0] = MOZART_CTL_VERSION;
    out[1] = MOZART_CTL_ERR;
    put_u16(out.data() + 2, 0x0002);   // response
    put_u32(out.data() + 4, req_id);
    put_u32(out.data() + 8, code);
    put_u32(out.data() + 12, static_cast<uint32_t>(text_len));
    std::memcpy(out.data() + 16, text, text_len);
    return out;
}

uint8_t mode_from_status(const nlohmann::json& status)
{
    const std::string mode = status.value("mode", "idle");
    if (mode == "realtime") return MOZART_CTL_MODE_REALTIME;
    if (mode == "file") return MOZART_CTL_MODE_FILE;
    return MOZART_CTL_MODE_IDLE;
}

uint8_t state_from_status(const nlohmann::json& status)
{
    // 状态语义与 /api/status 相同来源：ready 字段族。
    if (status.value("ready", false)) return MOZART_CTL_STATE_READY;
    if (status.value("failed", false)) return MOZART_CTL_STATE_FAILED;
    if (status.value("starting", false)) return MOZART_CTL_STATE_STARTING;
    return MOZART_CTL_STATE_DOWN;
}

} // namespace

ControlApiServer::ControlApiServer(const std::string& socket_path, ControlPlane* controller)
    : socket_path_(socket_path), controller_(controller) {}

ControlApiServer::~ControlApiServer()
{
    stop();
}

bool ControlApiServer::start()
{
    server_fd_ = ::socket(AF_UNIX, SOCK_DGRAM, 0);
    if (server_fd_ < 0) {
        spdlog::error("control_api socket: {}", std::strerror(errno));
        return false;
    }
    ::unlink(socket_path_.c_str());
    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    if (socket_path_.size() >= sizeof(addr.sun_path)) {
        spdlog::error("control_api 路径过长: {}", socket_path_);
        return false;
    }
    std::strncpy(addr.sun_path, socket_path_.c_str(), sizeof(addr.sun_path) - 1);
    if (::bind(server_fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
        spdlog::error("control_api bind {}: {}", socket_path_, std::strerror(errno));
        ::close(server_fd_);
        server_fd_ = -1;
        return false;
    }
    running_.store(true);
    server_thread_ = std::thread([this] { serve(); });
    spdlog::info("control_api 就绪: {}（协议 v{}, PING/STATUS/MODE_SET）",
                 socket_path_, MOZART_CTL_VERSION);
    return true;
}

void ControlApiServer::stop()
{
    if (!running_.exchange(false)) return;
    if (server_fd_ >= 0) {
        ::shutdown(server_fd_, 0);
        ::close(server_fd_);
        server_fd_ = -1;
    }
    if (server_thread_.joinable()) server_thread_.join();
    ::unlink(socket_path_.c_str());
}

void ControlApiServer::serve()
{
    std::array<unsigned char, kMaxDatagram> buffer{};
    while (running_.load()) {
        sockaddr_un peer{};
        socklen_t peer_len = sizeof(peer);
        const ssize_t n = ::recvfrom(server_fd_, buffer.data(), buffer.size(), 0,
                                    reinterpret_cast<sockaddr*>(&peer), &peer_len);
        if (n < 0) {
            if (errno == EINTR) continue;
            if (!running_.load()) break;
            spdlog::warn("control_api recvfrom: {}", std::strerror(errno));
            continue;
        }
        if (n < MOZART_CTL_HEADER_BYTES) continue;
        requests_.fetch_add(1);
        const uint8_t version = buffer[0];
        const uint8_t msg_type = buffer[1];
        const uint32_t req_id = get_u32(buffer.data() + 4);
        if (version != MOZART_CTL_VERSION) {
            auto reply = make_error(req_id, MOZART_CTL_ERR_UNKNOWN_MSG, "unsupported version");
            ::sendto(server_fd_, reply.data(), reply.size(), 0,
                     reinterpret_cast<sockaddr*>(&peer), peer_len);
            continue;
        }
        std::vector<unsigned char> reply;
        switch (msg_type) {
        case MOZART_CTL_PING: {
            reply.resize(8);
            reply[0] = MOZART_CTL_VERSION;
            reply[1] = MOZART_CTL_PONG;
            put_u16(reply.data() + 2, 0x0002);
            put_u32(reply.data() + 4, req_id);
            break;
        }
        case MOZART_CTL_STATUS: {
            const auto status = controller_ ? controller_->status() : nlohmann::json{};
            reply.resize(8 + 2 + 4 + 4);
            reply[0] = MOZART_CTL_VERSION;
            reply[1] = MOZART_CTL_STATUS_DATA;
            put_u16(reply.data() + 2, 0x0002);
            put_u32(reply.data() + 4, req_id);
            reply[8] = mode_from_status(status);
            reply[9] = state_from_status(status);
            put_u32(reply.data() + 10,
                    static_cast<uint32_t>(status.value("jobs_finished", 0u)));
            put_u32(reply.data() + 14,
                    static_cast<uint32_t>(status.value("jobs_failed", 0u)));
            break;
        }
        case MOZART_CTL_MODE_SET: {
            if (n < 9) {
                reply = make_error(req_id, MOZART_CTL_ERR_BAD_BODY, "mode_set needs 1 byte");
                break;
            }
            const uint8_t mode = buffer[8];
            const char* name = mode == MOZART_CTL_MODE_REALTIME ? "realtime"
                               : mode == MOZART_CTL_MODE_FILE ? "file"
                                                               : "idle";
            const auto result = controller_->request_mode(name, "");
            const bool accepted = result.value("ok", false) || result.value("accepted", false)
                                  || result.value("mode", "") == name;
            if (!accepted) {
                reply = make_error(req_id, MOZART_CTL_ERR_UNSUPPORTED,
                                   result.dump().substr(0, 200).c_str());
                break;
            }
            reply.resize(8 + 2);
            reply[0] = MOZART_CTL_VERSION;
            reply[1] = MOZART_CTL_MODE_EVT;
            put_u16(reply.data() + 2, 0x0002);
            put_u32(reply.data() + 4, req_id);
            reply[8] = mode;
            reply[9] = MOZART_CTL_STATE_STARTING;
            break;
        }
        default:
            reply = make_error(req_id, MOZART_CTL_ERR_UNKNOWN_MSG, "unknown msg_type");
            break;
        }
        ::sendto(server_fd_, reply.data(), reply.size(), 0,
                 reinterpret_cast<sockaddr*>(&peer), peer_len);
    }
}

} // namespace rvc
