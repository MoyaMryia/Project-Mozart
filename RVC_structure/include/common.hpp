#pragma once

#include <cstdint>
#include <cstring>
#include <vector>
#include <stdexcept>

// Platform-specific socket includes
#ifdef _WIN32
    #include <winsock2.h>
    #include <ws2tcpip.h>
    #pragma comment(lib, "ws2_32.lib")
#else
    #include <sys/socket.h>
    #include <netinet/in.h>
    #include <arpa/inet.h>
    #include <unistd.h>
    #include <fcntl.h>
    #include <errno.h>
#endif

namespace rvc {

// ──────────────────────────────────────────────────────────
// Endian helpers (little-endian packing)
// ──────────────────────────────────────────────────────────
inline uint32_t read_u32_le(const uint8_t* p) {
    return static_cast<uint32_t>(p[0])
         | (static_cast<uint32_t>(p[1]) << 8)
         | (static_cast<uint32_t>(p[2]) << 16)
         | (static_cast<uint32_t>(p[3]) << 24);
}

inline uint64_t read_u64_le(const uint8_t* p) {
    return static_cast<uint64_t>(read_u32_le(p))
         | (static_cast<uint64_t>(read_u32_le(p + 4)) << 32);
}

inline void write_u32_le(uint8_t* p, uint32_t v) {
    p[0] = static_cast<uint8_t>(v);
    p[1] = static_cast<uint8_t>(v >> 8);
    p[2] = static_cast<uint8_t>(v >> 16);
    p[3] = static_cast<uint8_t>(v >> 24);
}

inline void write_u64_le(uint8_t* p, uint64_t v) {
    write_u32_le(p, static_cast<uint32_t>(v));
    write_u32_le(p + 4, static_cast<uint32_t>(v >> 32));
}

// ──────────────────────────────────────────────────────────
// Socket helpers
// ──────────────────────────────────────────────────────────
inline bool set_socket_nonblocking(int fd) {
#ifdef _WIN32
    u_long mode = 1;
    return ioctlsocket(fd, FIONBIO, &mode) == 0;
#else
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0) return false;
    return fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0;
#endif
}

inline void socket_close(int fd) {
#ifdef _WIN32
    closesocket(fd);
#else
    close(fd);
#endif
}

} // namespace rvc
