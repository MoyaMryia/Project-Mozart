// control.c — 本地 UDP 控制通道实现
// ============================================================================
// 协议见 control.h。线程在收到合法命令后回调；非法命令回一行错误说明。
#define _POSIX_C_SOURCE 200809L
#include "mozart/control.h"

#include <arpa/inet.h>
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

struct mozart_control {
    int fd;
    pthread_t thread;
    bool started;
    const mozart_control_vt *vt;
    void *ctx;
};

static bool parse_on_off(const char *text, bool *out)
{
    if (!strcmp(text, "on"))  { *out = true;  return true; }
    if (!strcmp(text, "off")) { *out = false; return true; }
    return false;
}

// 处理一条命令。返回写入 reply 的字节数（含结尾 0）。
static size_t handle_line(char *line, char *reply, size_t reply_size,
                          const mozart_control_vt *vt, void *ctx)
{
    // 去掉行尾换行后按空白切两个字段
    char *end = strpbrk(line, "\r\n");
    if (end) *end = '\0';
    char *save = NULL;
    const char *cmd = strtok_r(line, " \t", &save);
    const char *arg = strtok_r(NULL, " \t", &save);
    const char *extra = strtok_r(NULL, " \t", &save);

    if (extra)
        return (size_t)snprintf(reply, reply_size, "err extra-argument\n") + 1;

    if (cmd && !strcmp(cmd, "mode") && arg) {
        if (!strcmp(arg, "ai")) { vt->set_route(ctx, 0); return (size_t)snprintf(reply, reply_size, "ok mode ai\n") + 1; }
        if (!strcmp(arg, "direct")) { vt->set_route(ctx, 1); return (size_t)snprintf(reply, reply_size, "ok mode direct\n") + 1; }
        return (size_t)snprintf(reply, reply_size, "err mode must be ai|direct\n") + 1;
    }
    if (cmd && !strcmp(cmd, "stage") && arg) {
        const char *value = strtok_r(NULL, " \t", &save);
        if (!value)
            return (size_t)snprintf(reply, reply_size, "err usage: stage <name> on|off\n") + 1;
        bool on = false;
        if (!parse_on_off(value, &on))
            return (size_t)snprintf(reply, reply_size, "err value must be on|off\n") + 1;
        if (!vt->set_stage(ctx, arg, on))
            return (size_t)snprintf(reply, reply_size,
                                    "err unknown stage %s (denoise|gate|agc|limiter)\n", arg) + 1;
        return (size_t)snprintf(reply, reply_size, "ok stage %s %s\n", arg, on ? "on" : "off") + 1;
    }
    if (cmd && !strcmp(cmd, "status") && !arg) {
        vt->snapshot(ctx, reply, reply_size);
        const size_t len = strlen(reply);
        if (len + 1 < reply_size && len > 0 && reply[len - 1] != '\n') {
            reply[len] = '\n';
            reply[len + 1] = '\0';
            return len + 2;
        }
        return len + 1;
    }
    return (size_t)snprintf(reply, reply_size,
                            "err commands: mode ai|direct | stage <name> on|off | status\n") + 1;
}

static void *control_thread(void *arg)
{
    mozart_control_t *c = arg;
    char buf[256];
    char reply[1024];
    for (;;) {
        struct sockaddr_in peer;
        socklen_t peer_len = sizeof(peer);
        const ssize_t n = recvfrom(c->fd, buf, sizeof(buf) - 1, 0,
                                   (struct sockaddr *)&peer, &peer_len);
        if (n < 0) {
            if (errno == EINTR) continue;
            break;  // close() 触发 EBADF/EINVAL，线程退出
        }
        buf[n] = '\0';
        size_t len = handle_line(buf, reply, sizeof(reply), c->vt, c->ctx);
        if (len > sizeof(reply)) len = sizeof(reply);  // snprintf 截断保护
        sendto(c->fd, reply, len, 0,
               (struct sockaddr *)&peer, peer_len);
    }
    return NULL;
}

mozart_control_t *mozart_control_open(uint16_t port,
                                      const mozart_control_vt *vt,
                                      void *ctx)
{
    if (port == 0 || !vt) return NULL;

    mozart_control_t *c = calloc(1, sizeof(*c));
    if (!c) return NULL;
    c->fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (c->fd < 0) { free(c); return NULL; }

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(c->fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        fprintf(stderr, "[ctl] bind 127.0.0.1:%u: %s\n", port, strerror(errno));
        close(c->fd);
        free(c);
        return NULL;
    }

    c->vt = vt;
    c->ctx = ctx;
    const int error = pthread_create(&c->thread, NULL, control_thread, c);
    if (error) {
        fprintf(stderr, "[ctl] pthread_create: %s\n", strerror(error));
        close(c->fd);
        free(c);
        return NULL;
    }
    c->started = true;
    fprintf(stderr, "[ctl] 控制通道 127.0.0.1:%u（mode/stage/status）\n", port);
    return c;
}

void mozart_control_close(mozart_control_t *c)
{
    if (!c) return;
    if (c->fd >= 0) {
        shutdown(c->fd, SHUT_RDWR);
        close(c->fd);
    }
    if (c->started) pthread_join(c->thread, NULL);
    free(c);
}
