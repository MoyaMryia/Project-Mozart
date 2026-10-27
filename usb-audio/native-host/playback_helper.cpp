#include "playback_helper.hpp"

#include "pipewire_pcm.hpp"
#include "runtime.hpp"

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include <sys/socket.h>
#include <unistd.h>

namespace mozart_host {
namespace {

bool send_byte(int fd, char value) {
    for (;;) {
        ssize_t n = ::send(fd, &value, 1, MSG_NOSIGNAL);
        if (n == 1) return true;
        if (n < 0 && errno == EINTR) continue;
        return false;
    }
}

int parse_uint(const char* text, unsigned& value) {
    char* end = nullptr;
    errno = 0;
    unsigned long parsed = std::strtoul(text, &end, 10);
    if (errno || !end || *end || parsed > 0xffffffffUL) return -1;
    value = unsigned(parsed);
    return 0;
}

} // namespace

int run_playback_helper(int argc, char** argv) {
    std::string target = "mozart_mic";
    unsigned block = 64, periods = 6, target_frames = 2;
    for (int i = 1; i < argc; ++i) {
        std::string key = argv[i];
        auto value = [&]() -> const char* {
            return i + 1 < argc ? argv[++i] : nullptr;
        };
        if (key == "--playback-helper") continue;
        if (key == "--pw-playback") {
            const char* v = value();
            if (!v) return 2;
            target = v;
        } else if (key == "--block") {
            const char* v = value();
            if (!v || parse_uint(v, block) != 0) return 2;
        } else if (key == "--periods") {
            const char* v = value();
            if (!v || parse_uint(v, periods) != 0) return 2;
        } else if (key == "--target") {
            const char* v = value();
            if (!v || parse_uint(v, target_frames) != 0) return 2;
        } else {
            std::fprintf(stderr, "playback helper: unknown option %s\n", key.c_str());
            return 2;
        }
    }
    if (!block || !periods || block > 4096 || periods < 3 || periods > 1024) return 2;

    try {
        Failure failure;
        auto xrun = [](bool, bool) {};
        PipeWirePcm playback(false, block, periods, target, failure, xrun);
        if (!send_byte(STDOUT_FILENO, 'R')) return 1;

        std::vector<uint8_t> data(size_t(block) * FrameBytes);
        for (;;) {
            ssize_t n = ::recv(STDIN_FILENO, data.data(), data.size(), 0);
            if (n == 0) break;
            if (n < 0) {
                if (errno == EINTR) continue;
                break;
            }
            if (size_t(n) != data.size()) {
                std::fprintf(stderr, "playback helper: short block %zd/%zu\n", n, data.size());
                send_byte(STDOUT_FILENO, 'E');
                return 1;
            }
            playback.transfer(data.data(), block);
            if (!send_byte(STDOUT_FILENO, 'A')) return 1;
        }
        playback.drain();
        return 0;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "playback helper failed: %s\n", e.what());
        send_byte(STDOUT_FILENO, 'E');
        return 1;
    }
}

} // namespace mozart_host
