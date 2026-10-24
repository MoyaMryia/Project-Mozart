// usb_link.c — 快流端点客户端实现（M3）
// ============================================================================
// 24 B 帧头与 usb-audio/proto.py 逐字段一致（小端）：
//   magic u16 0x4D5A | version u8 | type u8 | stream u8 | flags u8 |
//   reserved u16=0 | seq u32 | ts_us u64 | length u32
#define _POSIX_C_SOURCE 200809L
#include "mozart/usb_link.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define UL_MAGIC 0x4D5A
#define UL_VERSION 1
#define UL_TYPE_PCM 0
#define UL_STREAM_FAST 2
#define UL_HEADER_BYTES 24
#define UL_FAST_SAMPLES 128
#define UL_FAST_BYTES (UL_FAST_SAMPLES * 2 * 2)
#define UL_READ_CHUNK 65536

struct mozart_usblink {
    int fd_out;            // ep3：主机 → Jetson
    int fd_in;             // ep4：Jetson → 主机
    uint32_t tx_seq;
    uint64_t rx_frames, tx_frames;
    unsigned char pending[UL_READ_CHUNK * 2];
    size_t pending_len;
};

static void ul_pack_header(unsigned char *header, uint32_t seq, uint32_t length)
{
    uint64_t ts = 0;
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    ts = (uint64_t)now.tv_sec * 1000000ull + (uint64_t)now.tv_nsec / 1000;
    header[0] = UL_MAGIC & 0xff;
    header[1] = (UL_MAGIC >> 8) & 0xff;
    header[2] = UL_VERSION;
    header[3] = UL_TYPE_PCM;
    header[4] = UL_STREAM_FAST;
    header[5] = 0;                              // flags
    header[6] = header[7] = 0;                  // reserved
    memcpy(header + 8, &seq, 4);
    memcpy(header + 12, &ts, 8);
    memcpy(header + 20, &length, 4);
}

mozart_usblink_t *mozart_usblink_open(const char *ffs_dir)
{
    if (!ffs_dir) return NULL;
    mozart_usblink_t *link = calloc(1, sizeof(*link));
    if (!link) return NULL;
    char path[512];
    snprintf(path, sizeof(path), "%s/ep3", ffs_dir);
    link->fd_out = open(path, O_RDONLY);
    if (link->fd_out < 0) {
        fprintf(stderr, "[usb] 打开 %s 失败: %s\n", path, strerror(errno));
        free(link);
        return NULL;
    }
    snprintf(path, sizeof(path), "%s/ep4", ffs_dir);
    link->fd_in = open(path, O_WRONLY);
    if (link->fd_in < 0) {
        fprintf(stderr, "[usb] 打开 %s 失败: %s\n", path, strerror(errno));
        close(link->fd_out);
        free(link);
        return NULL;
    }
    return link;
}

// 从 pending 解出一帧快流音频；数据不足或头非法时返回 0 / -1。
static int ul_pop_frame(mozart_usblink_t *link, int16_t *stereo, int capacity)
{
    if (link->pending_len < UL_HEADER_BYTES) return 0;
    const unsigned char *header = link->pending;
    const uint16_t magic = (uint16_t)(header[0] | header[1] << 8);
    if (magic != UL_MAGIC) {
        fprintf(stderr, "[usb] 帧头 magic 异常 0x%02x%02x，丢弃缓冲\n",
                header[1], header[0]);
        return -1;
    }
    uint32_t length;
    memcpy(&length, header + 20, 4);
    if (header[3] != UL_TYPE_PCM || header[4] != UL_STREAM_FAST) {
        // 未知类型/流：按长度跳过该帧（前向兼容，长度字段独立于流类型）。
        if (length > 0x100000) return -1;
        if (link->pending_len < UL_HEADER_BYTES + length) return 0;
        memmove(link->pending, link->pending + UL_HEADER_BYTES + length,
                link->pending_len - UL_HEADER_BYTES - length);
        link->pending_len -= UL_HEADER_BYTES + length;
        return 0;
    }
    if (length != UL_FAST_BYTES) return -1;   // 快流尺寸固定
    if (link->pending_len < UL_HEADER_BYTES + length) return 0;
    if (capacity < UL_FAST_SAMPLES) return -1;
    memcpy(stereo, link->pending + UL_HEADER_BYTES, length);
    memmove(link->pending, link->pending + UL_HEADER_BYTES + length,
            link->pending_len - UL_HEADER_BYTES - length);
    link->pending_len -= UL_HEADER_BYTES + length;
    link->rx_frames++;
    return UL_FAST_SAMPLES;
}

int mozart_usblink_read_audio(mozart_usblink_t *link, int16_t *stereo, int capacity_frames)
{
    if (!link || !stereo || capacity_frames < UL_FAST_SAMPLES) return -1;
    for (;;) {
        const int popped = ul_pop_frame(link, stereo, capacity_frames);
        if (popped > 0) return popped;
        if (popped < 0) return -1;
        const ssize_t n = read(link->fd_out, link->pending + link->pending_len,
                               UL_READ_CHUNK);
        if (n < 0) {
            if (errno == EINTR) continue;
            fprintf(stderr, "[usb] 读 ep3 失败: %s\n", strerror(errno));
            return -1;
        }
        if (n == 0) continue;
        link->pending_len += (size_t)n;
    }
}

int mozart_usblink_write_audio(mozart_usblink_t *link, const int16_t *stereo, int frames)
{
    if (!link || !stereo || frames != UL_FAST_SAMPLES) return -1;
    unsigned char frame[UL_HEADER_BYTES + UL_FAST_BYTES];
    ul_pack_header(frame, link->tx_seq++, UL_FAST_BYTES);
    memcpy(frame + UL_HEADER_BYTES, stereo, UL_FAST_BYTES);
    size_t done = 0;
    while (done < sizeof(frame)) {
        const ssize_t n = write(link->fd_in, frame + done, sizeof(frame) - done);
        if (n < 0) {
            if (errno == EINTR) continue;
            fprintf(stderr, "[usb] 写 ep4 失败: %s\n", strerror(errno));
            return -1;
        }
        done += (size_t)n;
    }
    link->tx_frames++;
    return frames;
}

uint64_t mozart_usblink_rx_frames(const mozart_usblink_t *link)
{
    return link ? link->rx_frames : 0;
}

uint64_t mozart_usblink_tx_frames(const mozart_usblink_t *link)
{
    return link ? link->tx_frames : 0;
}

void mozart_usblink_close(mozart_usblink_t *link)
{
    if (!link) return;
    if (link->fd_out >= 0) close(link->fd_out);
    if (link->fd_in >= 0) close(link->fd_in);
    free(link);
}
