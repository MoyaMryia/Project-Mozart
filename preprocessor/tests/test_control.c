// test_control.c — 控制通道应答格式
// ============================================================================
// status 应答是一行 JSON 加换行。应答不得附带结尾 NUL，否则按文本解析的
// 调用方（前端、脚本）会在 JSON 之后读到多余字节。
#include "mozart/control.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#define CHECK(cond) do { \
    if (!(cond)) { \
        fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        failures++; \
    } \
} while (0)

#define TEST_PORT 19555
#define SNAPSHOT "{\"mode\":\"ai\",\"frames\":1}"

static void snap(void *ctx, char *buf, size_t size)
{
    (void)ctx;
    snprintf(buf, size, "%s", SNAPSHOT);
}

static void set_route(void *ctx, int route)
{
    (void)ctx;
    (void)route;
}

static bool set_stage(void *ctx, const char *name, bool on)
{
    (void)ctx;
    (void)name;
    (void)on;
    return true;
}

static size_t roundtrip(const char *cmd, char *out, size_t cap)
{
    const int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) return 0;
    struct timeval tv = { .tv_sec = 2, .tv_usec = 0 };
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    struct sockaddr_in peer;
    memset(&peer, 0, sizeof(peer));
    peer.sin_family = AF_INET;
    peer.sin_port = htons(TEST_PORT);
    peer.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    const ssize_t sent = sendto(fd, cmd, strlen(cmd), 0,
                                (struct sockaddr *)&peer, sizeof(peer));
    ssize_t got = 0;
    if (sent == (ssize_t)strlen(cmd))
        got = recvfrom(fd, out, cap, 0, NULL, NULL);
    close(fd);
    return got > 0 ? (size_t)got : 0;
}

int main(void)
{
    int failures = 0;
    const mozart_control_vt vt = {
        .set_route = set_route,
        .set_stage = set_stage,
        .snapshot = snap,
    };
    mozart_control_t *ctl = mozart_control_open(TEST_PORT, &vt, NULL);
    if (!ctl) {
        fprintf(stderr, "control port %d unavailable\n", TEST_PORT);
        return 1;
    }

    char out[1024];
    size_t n = roundtrip("status", out, sizeof(out));
    // 恰好是 JSON 加一个换行；多一个字节说明把结尾 NUL 也发了。
    CHECK(n == strlen(SNAPSHOT) + 1);
    CHECK(n > 0 && out[n - 1] == '\n');
    CHECK(memchr(out, '\0', n) == NULL);
    CHECK(n >= strlen(SNAPSHOT) && memcmp(out, SNAPSHOT, strlen(SNAPSHOT)) == 0);

    n = roundtrip("stage gate on", out, sizeof(out));
    CHECK(n == strlen("ok stage gate on\n"));
    CHECK(n > 0 && memcmp(out, "ok stage gate on\n", n) == 0);

    n = roundtrip("mode direct", out, sizeof(out));
    CHECK(n == strlen("ok mode direct\n"));
    CHECK(n > 0 && memcmp(out, "ok mode direct\n", n) == 0);

    mozart_control_close(ctl);

    if (failures) {
        fprintf(stderr, "test_control: %d failure(s)\n", failures);
        return 1;
    }
    printf("test_control ok\n");
    return 0;
}
