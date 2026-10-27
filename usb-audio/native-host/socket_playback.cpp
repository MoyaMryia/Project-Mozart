#include "socket_playback.hpp"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <stdexcept>
#include <thread>

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

namespace mozart_host {
namespace {

constexpr unsigned PollMs = 20;
constexpr auto Timeout = std::chrono::milliseconds(3 * MOZART_USB_IO_TIMEOUT_MS);

void set_nonblocking(int fd) {
    int flags = fcntl(fd, F_GETFL, 0);
    require(flags >= 0, "fcntl(F_GETFL) failed");
    require(fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0, "fcntl(O_NONBLOCK) failed");
}

} // namespace

struct SocketPlayback::Impl {
    unsigned block;
    unsigned periods;
    unsigned target_frames;
    std::string target;
    Failure& failure;
    int fd = -1;
    pid_t pid = -1;
    uint64_t sent = 0;
    uint64_t acked = 0;
    bool failed = false;

    Impl(unsigned block_, unsigned periods_, unsigned target_, std::string target_name, Failure& failure_)
        : block(block_), periods(periods_), target_frames(target_), target(std::move(target_name)),
          failure(failure_) {}

    void read_acks() {
        for (;;) {
            char value = 0;
            ssize_t n = ::recv(fd, &value, 1, MSG_DONTWAIT);
            if (n == 1) {
                if (value == 'A') { ++acked; continue; }
                if (value == 'E') {
                    failed = true;
                    throw std::runtime_error("playback helper reported an error");
                }
                continue;
            }
            if (n == 0) {
                failed = true;
                throw std::runtime_error("playback helper exited");
            }
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) return;
            failed = true;
            throw std::runtime_error(std::string("playback helper recv: ") + std::strerror(errno));
        }
    }

    void wait_ready() {
        auto deadline = std::chrono::steady_clock::now() + Timeout;
        for (;;) {
            auto left = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now()).count();
            require(left > 0, "playback helper ready timeout");
            pollfd p{fd, POLLIN, 0};
            int rc = ::poll(&p, 1, int(left));
            if (rc < 0 && errno == EINTR) continue;
            require(rc > 0, "playback helper ready timeout");
            char value = 0;
            ssize_t n = ::recv(fd, &value, 1, 0);
            require(n == 1, "playback helper closed before ready");
            if (value == 'R') return;
            require(value != 'E', "playback helper failed to start");
        }
    }
};

SocketPlayback::SocketPlayback(unsigned block, unsigned periods, unsigned target_frames,
                               std::string target, Failure& failure)
    : impl_(new Impl(block, periods, target_frames, std::move(target), failure)) {
    Impl& i = *impl_;
    int sockets[2] = {-1, -1};
    require(::socketpair(AF_UNIX, SOCK_SEQPACKET, 0, sockets) == 0,
            std::string("playback socketpair: ") + std::strerror(errno));
    int sndbuf = 8192;
    ::setsockopt(sockets[0], SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));
    set_nonblocking(sockets[0]);

    std::string block_text = std::to_string(block);
    std::string periods_text = std::to_string(std::max(periods, 32u));
    std::string target_text = std::to_string(target_frames);
    i.pid = ::fork();
    require(i.pid >= 0, std::string("playback fork: ") + std::strerror(errno));
    if (i.pid == 0) {
        ::close(sockets[0]);
        ::dup2(sockets[1], STDIN_FILENO);
        ::dup2(sockets[1], STDOUT_FILENO);
        if (sockets[1] > STDERR_FILENO) ::close(sockets[1]);
        for (int fd = 3; fd < 1024; ++fd) ::close(fd);
        ::execl("/proc/self/exe", "mozart-usb-host", "--playback-helper",
                "--pw-playback", i.target.c_str(), "--block", block_text.c_str(),
                "--periods", periods_text.c_str(), "--target", target_text.c_str(),
                static_cast<char*>(nullptr));
        _exit(127);
    }
    ::close(sockets[1]);
    i.fd = sockets[0];
    try {
        i.wait_ready();
    } catch (...) {
        ::close(i.fd); i.fd = -1;
        ::kill(i.pid, SIGKILL);
        ::waitpid(i.pid, nullptr, 0);
        throw;
    }
}

SocketPlayback::~SocketPlayback() {
    Impl& i = *impl_;
    if (i.fd >= 0) {
        ::shutdown(i.fd, SHUT_WR);
        ::close(i.fd);
        i.fd = -1;
    }
    if (i.pid > 0) {
        int status = 0;
        for (int n = 0; n < 30; ++n) {
            pid_t rc = ::waitpid(i.pid, &status, WNOHANG);
            if (rc == i.pid) { i.pid = -1; return; }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        ::kill(i.pid, SIGTERM);
        ::waitpid(i.pid, &status, 0);
        i.pid = -1;
    }
}

void SocketPlayback::start() {}
void SocketPlayback::stop_capture() {}

void SocketPlayback::set_target(unsigned frames) { impl_->target_frames = frames; }

int64_t SocketPlayback::delay() {
    Impl& i = *impl_;
    if (i.fd >= 0) i.read_acks();
    return int64_t((i.sent - i.acked) * i.block);
}

void SocketPlayback::wait_target() {
    Impl& i = *impl_;
    const uint64_t target_blocks = std::max<uint64_t>(1, (i.target_frames + i.block - 1) / i.block);
    auto deadline = std::chrono::steady_clock::now() + Timeout;
    for (;;) {
        i.failure.check();
        i.read_acks();
        if (i.sent - i.acked <= target_blocks) return;
        auto left = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now()).count();
        require(left > 0, "playback helper target timeout");
        pollfd p{i.fd, POLLIN, 0};
        int rc = ::poll(&p, 1, int(std::min<long long>(left, PollMs)));
        if (rc < 0 && errno == EINTR) continue;
    }
}

void SocketPlayback::transfer(uint8_t* data, unsigned frames) {
    Impl& i = *impl_;
    const size_t bytes = size_t(frames) * FrameBytes;
    require(bytes == size_t(i.block) * FrameBytes, "playback block size mismatch");
    auto deadline = std::chrono::steady_clock::now() + Timeout;
    for (;;) {
        i.failure.check();
        ssize_t n = ::send(i.fd, data, bytes, MSG_NOSIGNAL);
        if (n == ssize_t(bytes)) { ++i.sent; return; }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            auto left = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now()).count();
            require(left > 0, "playback helper send timeout");
            pollfd p{i.fd, POLLOUT, 0};
            int rc = ::poll(&p, 1, int(std::min<long long>(left, PollMs)));
            if (rc < 0 && errno == EINTR) continue;
            continue;
        }
        if (n < 0 && errno == EINTR) continue;
        i.failed = true;
        throw std::runtime_error(std::string("playback helper send: ") + std::strerror(errno));
    }
}

void SocketPlayback::drain() {
    Impl& i = *impl_;
    auto deadline = std::chrono::steady_clock::now() + Timeout;
    for (;;) {
        i.failure.check();
        i.read_acks();
        if (i.acked == i.sent) break;
        auto left = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now()).count();
        require(left > 0, "playback helper drain timeout");
        pollfd p{i.fd, POLLIN, 0};
        int rc = ::poll(&p, 1, int(std::min<long long>(left, PollMs)));
        if (rc < 0 && errno == EINTR) continue;
    }
    if (i.fd >= 0) {
        ::shutdown(i.fd, SHUT_WR);
    }
}

bool SocketPlayback::running() {
    Impl& i = *impl_;
    if (i.failed || i.pid <= 0 || i.fd < 0) return false;
    int status = 0;
    pid_t rc = ::waitpid(i.pid, &status, WNOHANG);
    if (rc == 0) return true;
    i.pid = -1;
    return false;
}

} // namespace mozart_host
