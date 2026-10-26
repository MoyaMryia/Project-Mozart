// main.c — mozart-pre：采集 → 单干增强 → MZRT 发送 + 直出路由
// ============================================================================
// 实时模式：
//   mozart-pre [-d 采集设备] [-o 播放设备] [-a 目标IP] [-p 端口] [-b 第二目标]
//              [--period-frames N] [--mode ai|direct] [--ctl-port N]
//              [--no-denoise] [--gate] [--agc] [--limiter]
// 离线模式（48k/立体声/PCM16 WAV 代替麦克风）：
//   mozart-pre -i 输入.wav [--pace] [其余参数同上]
//
// 单干（M1）：采集 → FL 单声道 → 高通 → WOLA 谱降噪 → 整形级 → 主干输出。
//   主干 → 960 样本累积 → 16k 契约帧 → MZRT UDP（恒定发送，AI 路径永不停）。
//   直出 = 播放路由开关：direct 模式把主干写播放设备，后端回包被丢弃计数。
// 控制通道（--ctl-port，默认 19040，0 关闭）：mode / stage / status。
#define _POSIX_C_SOURCE 200809L
#include "mozart.h"
#include "mozart/playback.h"
#include "mozart/control.h"
#include "mozart/dsp.h"
#include "mozart/usb_link.h"
#include "mozart/usb_protocol_generated.h"

#include <arpa/inet.h>
#include <errno.h>
#include <netdb.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t g_stop = 0;

static void on_signal(int sig) { (void)sig; g_stop = 1; }

// ---- 播放路由与控制共享状态 -------------------------------------------------

enum { ROUTE_AI = 0, ROUTE_DIRECT = 1 };

typedef struct {
    atomic_int  route_req;      // 控制线程写：请求路由
    atomic_int  route_cur;      // 主线程写：当前生效路由（播放线程读）
    atomic_int  want_denoise, want_gate, want_agc, want_limiter;
    atomic_ulong skipped_replies;
    atomic_ulong direct_periods;
    atomic_llong direct_us_last, direct_us_max;
    _Atomic double direct_us_avg;
    pthread_mutex_t status_mutex;
    char status[2048];
    atomic_int status_error;
    char status_error_reason[256]; // 写线程先保存原因，再发布 status_error。
    bool usb_input;
} shared_t;

typedef struct {
    int                 fd;         // 已连接的后端 UDP socket（收发同口）
    mozart_playback_t   *playback;
    shared_t            *shared;
    atomic_bool         stop;
    atomic_int          error;
    atomic_uint         last_played_frame;
    bool                monotonic_pts;
} playback_args_t;

static bool io_should_stop(void *context)
{
    playback_args_t *pa = context;
    return g_stop || atomic_load(&pa->stop) || atomic_load(&pa->error) != 0
           || atomic_load(&pa->shared->status_error) != 0;
}

// ---- 播放线程：收 MZRT 输出包 → ALSA（direct 模式丢弃计数） -----------------

static void *playback_thread(void *arg)
{
    playback_args_t *pa = arg;
    mozart_playback_t *pb = pa->playback;
    unsigned char pkt[4 + sizeof(mozart_output_frame_t)];
    long received = 0, played = 0, bad_packets = 0;
    double latency_sum_ms = 0.0, latency_max_ms = 0.0;
    long latency_count = 0;
    uint64_t t0 = mozart_now_ns();

    while (!io_should_stop(pa)) {
        ssize_t n = recv(pa->fd, pkt, sizeof(pkt), 0);
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) continue;
            // 后端重启窗口内，已连接 UDP 套接字会收到 ICMP 拒绝；可恢复，保持监听。
            if (errno == ECONNREFUSED || errno == EHOSTUNREACH
                || errno == ENETUNREACH) continue;
            if (!io_should_stop(pa)) {
                atomic_store(&pa->error, -errno);
                perror("[play] status=failed stage=recv");
            }
            break;
        }
        if (n != (ssize_t)sizeof(pkt)) {
            if (n > 0) bad_packets++;
            continue;
        }
        if (pkt[0] != 0x54 || pkt[1] != 0x52 || pkt[2] != 0x5A || pkt[3] != 0x4D) {
            bad_packets++;
            continue;
        }
        mozart_output_frame_t frame;
        memcpy(&frame, pkt + 4, sizeof(frame));
        received++;

        if (atomic_load(&pa->shared->route_cur) == ROUTE_DIRECT) {
            // 直出模式下回包路由到丢弃计数；帧号仍推进以维持排空判定。
            atomic_fetch_add(&pa->shared->skipped_replies, 1);
            atomic_store(&pa->last_played_frame, frame.meta.frame_idx);
            continue;
        }

        const int error = mozart_playback_write(pb, frame.pcm, MOZART_OUTPUT_SAMPLES);
        if (error < 0) {
            if (error != -ECANCELED || !io_should_stop(pa)) {
                atomic_store(&pa->error, error);
                fprintf(stderr, "[play] status=failed stage=write code=%d\n", error);
            }
            break;
        }
        played++;
        atomic_store(&pa->last_played_frame, frame.meta.frame_idx);

        // 回包携带当前输入帧的元数据，不能代表缓冲后音频的端到端延迟。
        if (pa->monotonic_pts && frame.meta.pts_ns > 1000000000ull) {
            const uint64_t now = mozart_now_ns();
            const double lat_ms = (double)(now - frame.meta.pts_ns) / 1e6;
            latency_sum_ms += lat_ms;
            if (lat_ms > latency_max_ms) latency_max_ms = lat_ms;
            latency_count++;
        }

        if (played % 250 == 0 && played > 0) {
            fprintf(stderr,
                "[play] played=%ld/%ld bad=%ld underruns=%ld skipped=%lu packet_age_avg=%.1fms "
                "packet_age_max=%.1fms elapsed=%.1fs\n",
                played, received, bad_packets,
                mozart_playback_underruns(pb),
                atomic_load(&pa->shared->skipped_replies),
                latency_count ? latency_sum_ms / latency_count : 0.0,
                latency_max_ms,
                (mozart_now_ns() - t0) / 1e9);
        }
    }

    fprintf(stderr,
        "[play] 结束: 播放 %ld/%ld 包, bad=%ld, underruns=%ld, skipped=%lu\n",
        played, received, bad_packets, mozart_playback_underruns(pb),
        atomic_load(&pa->shared->skipped_replies));
    return NULL;
}

// ---- 控制通道回调（控制线程执行，只做原子写与加锁读） ------------------------

static bool ctl_set_route(void *ctx, int route)
{
    shared_t *shared = ctx;
    if (shared->usb_input && route == ROUTE_AI) return false;
    atomic_store(&shared->route_req, route ? ROUTE_DIRECT : ROUTE_AI);
    return true;
}

static bool ctl_set_stage(void *ctx, const char *name, bool on)
{
    shared_t *shared = ctx;
    if (!strcmp(name, "denoise")) atomic_store(&shared->want_denoise, on);
    else if (!strcmp(name, "gate")) atomic_store(&shared->want_gate, on);
    else if (!strcmp(name, "agc")) atomic_store(&shared->want_agc, on);
    else if (!strcmp(name, "limiter")) atomic_store(&shared->want_limiter, on);
    else return false;
    return true;
}

static void ctl_snapshot(void *ctx, char *buf, size_t size)
{
    shared_t *shared = ctx;
    char snapshot[sizeof(shared->status)];
    pthread_mutex_lock(&shared->status_mutex);
    memcpy(snapshot, shared->status, sizeof(snapshot));
    pthread_mutex_unlock(&shared->status_mutex);
    snprintf(buf, size, "%s", snapshot);
}

typedef struct {
    long long last, max;
    double avg;
} processing_t;

// JSON 字符串逐字节转义，错误原文中的引号和控制字符不破坏快照。
static void json_escape(const char *src, char *dst, size_t size)
{
    size_t n = 0;
    for (const unsigned char *p = (const unsigned char *)src; *p; p++) {
        const size_t need = *p < 32 ? 6 : (*p == '"' || *p == '\\' ? 2 : 1);
        if (n + need >= size) break;
        if (*p < 32) {
            snprintf(dst + n, size - n, "\\u%04x", *p);
        } else {
            if (need == 2) dst[n++] = '\\';
            dst[n] = (char)*p;
        }
        n += *p < 32 ? 6 : 1;
    }
    dst[n] = '\0';
}

static void update_status(shared_t *s, mozart_dsp_t *dsp, mozart_usblink_t *usb,
                          const char *state, const char *reason, long sent,
                          long send_errors, unsigned conf, const processing_t *processing)
{
    mozart_dsp_telemetry_t tm;
    mozart_dsp_telemetry(dsp, &tm);
    char escaped[768];
    json_escape(reason, escaped, sizeof(escaped));
    char snapshot[sizeof(s->status)] = {0};
    snprintf(snapshot, sizeof(snapshot),
        "{\"mode\":\"%s\",\"stages\":{\"denoise\":%d,\"gate\":%d,\"agc\":%d,"
        "\"limiter\":%d},\"frames\":%ld,\"direct_periods\":%lu,"
        "\"skipped_replies\":%lu,\"send_errors\":%ld,"
        "\"direct_us\":{\"last\":%lld,\"max\":%lld,\"avg\":%.0f},"
        "\"processing_us\":{\"last\":%lld,\"max\":%lld,\"avg\":%.0f},"
        "\"ns_gain\":%.3f,\"gate_open\":%ld,\"vad_conf\":%.3f,"
        "\"state\":\"%s\",\"reason\":\"%s\",\"status_errno\":%d,"
        "\"usb\":{\"block\":%d,\"negotiated\":%s,\"session\":%llu,"
        "\"rx\":%llu,\"tx\":%llu,\"pending\":%zu}}",
        atomic_load(&s->route_cur) == ROUTE_DIRECT ? "direct" : "ai",
        tm.denoise, tm.gate, tm.agc, tm.limiter, sent,
        (unsigned long)atomic_load(&s->direct_periods),
        (unsigned long)atomic_load(&s->skipped_replies), send_errors,
        (long long)atomic_load(&s->direct_us_last),
        (long long)atomic_load(&s->direct_us_max), atomic_load(&s->direct_us_avg),
        processing->last, processing->max, processing->avg,
        (double)tm.ns_last_gain, (long)tm.gate_open, conf / 255.0,
        state, escaped, atomic_load(&s->status_error), mozart_usblink_block_frames(usb),
        mozart_usblink_negotiated(usb) ? "true" : "false",
        (unsigned long long)mozart_usblink_generation(usb),
        (unsigned long long)mozart_usblink_rx_frames(usb),
        (unsigned long long)mozart_usblink_tx_frames(usb),
        mozart_usblink_pending_frames(usb));
    pthread_mutex_lock(&s->status_mutex);
    memcpy(s->status, snapshot, sizeof(snapshot));
    pthread_mutex_unlock(&s->status_mutex);
}

// 临时文件与目标文件位于同一目录，rename 原子替换最终快照。
static int persist_status(const char *path, const char *snapshot)
{
    char *temp = malloc(strlen(path) + sizeof(".XXXXXX"));
    if (!temp) return ENOMEM;
    sprintf(temp, "%s.XXXXXX", path);
    int fd = mkstemp(temp);
    int error = fd < 0 ? errno : 0;
    if (fd >= 0) {
        if (fchmod(fd, 0644) < 0) error = errno;
        const char *p = snapshot;
        size_t left = strlen(p);
        while (!error && left) {
            ssize_t n = write(fd, p, left);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) { error = n < 0 ? errno : EIO; break; }
            p += n;
            left -= (size_t)n;
        }
        if (close(fd) < 0) {
            const int close_error = errno;
            fprintf(stderr, "[pre] status=failed stage=status_close errno=%d reason=%s\n",
                    close_error, strerror(close_error));
            if (!error) error = close_error;
        }
        if (!error && rename(temp, path) < 0) error = errno;
        if (error && unlink(temp) < 0)
            fprintf(stderr, "[pre] status cleanup unlink: %s\n", strerror(errno));
    }
    free(temp);
    return error;
}

enum { STATUS_WRITE_INTERVAL_NS = 100000000 };

typedef struct {
    shared_t *shared;
    const char *path;
    atomic_bool stop;
} status_writer_t;

static void status_writer_fail(shared_t *shared, const char *stage, int error)
{
    snprintf(shared->status_error_reason, sizeof(shared->status_error_reason),
             "%s: %s", stage, strerror(error));
    atomic_store(&shared->status_error, error);
    fprintf(stderr, "[pre] status=failed stage=%s errno=%d reason=%s\n",
            stage, error, shared->status_error_reason);
}

static void *status_writer_thread(void *context)
{
    status_writer_t *writer = context;
    uint64_t next = mozart_now_ns();
    while (!atomic_load(&writer->stop)) {
        char snapshot[sizeof(writer->shared->status)];
        ctl_snapshot(writer->shared, snapshot, sizeof(snapshot));
        const int error = persist_status(writer->path, snapshot);
        if (error) {
            status_writer_fail(writer->shared, "status_file", error);
            break;
        }
        // 跳过慢写占用的周期，避免补写已过期的快照。
        const uint64_t now = mozart_now_ns();
        next += ((now - next) / STATUS_WRITE_INTERVAL_NS + 1) * STATUS_WRITE_INTERVAL_NS;
        const struct timespec wake = {
            .tv_sec = (time_t)(next / 1000000000ull),
            .tv_nsec = (long)(next % 1000000000ull),
        };
        int wait_error;
        do {
            wait_error = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &wake, NULL);
        } while (wait_error == EINTR && !atomic_load(&writer->stop));
        if (wait_error && wait_error != EINTR) {
            status_writer_fail(writer->shared, "status_clock", wait_error);
            break;
        }
    }
    return NULL;
}

// join 失败时保留线程使用的资源，并立即以失败状态退出进程。
static void join_or_exit(pthread_t thread, const char *stage)
{
    const int error = pthread_join(thread, NULL);
    if (error) {
        fprintf(stderr, "[pre] status=failed stage=%s reason=%s\n", stage, strerror(error));
        _Exit(EXIT_FAILURE);
    }
}

static int udp_open(const char *host, uint16_t port)
{
    struct addrinfo hints = {0}, *res = NULL;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_DGRAM;
    char port_str[6];
    snprintf(port_str, sizeof(port_str), "%u", port);
    if (getaddrinfo(host, port_str, &hints, &res) != 0 || !res) {
        fprintf(stderr, "[net] 解析 %s 失败\n", host);
        return -1;
    }
    int fd = socket(res->ai_family, res->ai_socktype, 0);
    if (fd >= 0 && connect(fd, res->ai_addr, res->ai_addrlen) < 0) {
        close(fd);
        fd = -1;
    }
    freeaddrinfo(res);
    if (fd < 0) {
        perror("[net] socket/connect");
        return -1;
    }
    return fd;
}

static void usage(const char *prog)
{
    fprintf(stderr,
        "用法: %s [-d alsa采集设备] [-o alsa播放设备] [-a 目标IP] [-p 端口] [-n 帧数] "
        "[-i 输入.wav] [--pace] [--period-frames N] [--mode ai|direct] [--ctl-port N] "
        "[--no-denoise] [--gate] [--agc] [--limiter] [--status-file PATH]\n"
        "  -d   ALSA 采集设备（默认 default）\n"
        "  -o   ALSA 播放设备；缺省只发送不播放\n"
        "  --period-frames  采集/直出周期（默认 64，≤960；AI 路径仍按 960 攒帧）\n"
        "  --mode           初始播放路由（USB 默认 direct，其余默认 ai）\n"
        "  --status-file    原子写入状态快照；默认只提供控制口查询\n"
        "  --ctl-port       本地控制通道 UDP 端口（默认 19040；0 关闭）\n"
        "  --no-denoise     关闭 WOLA 谱降噪（旁路）\n",
        prog);
}

// ---- 直出淡入淡出 -----------------------------------------------------------

typedef struct {
    float gain;        // 当前直出增益
    int   direction;   // +1 淡入 / -1 淡出 / 0 稳态
    int   remaining;   // 剩余淡变样本
    int   fade_len;
} fade_t;

static void fade_init(fade_t *f, int fade_len, bool start_active)
{
    f->fade_len = fade_len;
    f->gain = start_active ? 1.0f : 0.0f;
    f->direction = 0;
    f->remaining = 0;
}

static void fade_begin(fade_t *f, int direction)
{
    f->direction = direction;
    f->remaining = f->fade_len;
}

// 对一段立体声 S16 应用当前淡变；离开方向完成后返回 false 表示可以切路由。
static bool fade_apply(fade_t *f, int16_t *stereo, size_t frames)
{
    if (!f->direction) return true;
    const float step = 1.0f / (float)f->fade_len;
    for (size_t i = 0; i < frames; i++) {
        if (f->remaining > 0) {
            f->gain += step * (float)f->direction;
            if (f->gain > 1.0f) f->gain = 1.0f;
            if (f->gain < 0.0f) f->gain = 0.0f;
            f->remaining--;
        }
        stereo[2 * i]     = (int16_t)(stereo[2 * i] * f->gain);
        stereo[2 * i + 1] = (int16_t)(stereo[2 * i + 1] * f->gain);
    }
    if (f->remaining <= 0) {
        if (f->direction < 0) {
            f->direction = 0;
            return false;   // 淡出完成，可切回 AI
        }
        f->direction = 0;
    }
    return true;
}

int main(int argc, char **argv)
{
    const char *device = "default";
    const char *play_device = NULL;
    const char *host = "127.0.0.1";
    const char *second_target = NULL;
    const char *wav_path = NULL;
    const char *usb_ffs = NULL;
    const char *status_file = NULL;
    bool mode_explicit = false;
    bool denoise = true, gate_on = false, agc_on = false, limiter_on = false;
    bool pace = false;
    uint16_t port = 18000;
    uint16_t ctl_port = 19040;
    unsigned period_frames = 64;   // 2.67ms→1.33ms：≤10ms 端到端口径的余量（见 e2e 报告）
    int initial_route = ROUTE_AI;
    int fade_ms = 10;
    long max_frames = -1;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-d") && i + 1 < argc) device = argv[++i];
        else if (!strcmp(argv[i], "-o") && i + 1 < argc) play_device = argv[++i];
        else if (!strcmp(argv[i], "-a") && i + 1 < argc) host = argv[++i];
        else if (!strcmp(argv[i], "-p") && i + 1 < argc) port = (uint16_t)atoi(argv[++i]);
        else if (!strcmp(argv[i], "-b") && i + 1 < argc) second_target = argv[++i];
        else if (!strcmp(argv[i], "-n") && i + 1 < argc) max_frames = atol(argv[++i]);
        else if (!strcmp(argv[i], "-i") && i + 1 < argc) wav_path = argv[++i];
        else if (!strcmp(argv[i], "--pace")) pace = true;
        else if (!strcmp(argv[i], "--no-denoise")) denoise = false;
        else if (!strcmp(argv[i], "--gate")) gate_on = true;
        else if (!strcmp(argv[i], "--agc")) agc_on = true;
        else if (!strcmp(argv[i], "--limiter")) limiter_on = true;
        else if (!strcmp(argv[i], "--period-frames") && i + 1 < argc) period_frames = (unsigned)atoi(argv[++i]);
        else if (!strcmp(argv[i], "--ctl-port") && i + 1 < argc) ctl_port = (uint16_t)atoi(argv[++i]);
        else if (!strcmp(argv[i], "--mode") && i + 1 < argc) {
            mode_explicit = true;
            if (!strcmp(argv[++i], "direct")) initial_route = ROUTE_DIRECT;
            else if (!strcmp(argv[i], "ai")) initial_route = ROUTE_AI;
            else { usage(argv[0]); return 1; }
        }
        else if (!strcmp(argv[i], "--fade-ms") && i + 1 < argc) fade_ms = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--usb-endpoint") && i + 1 < argc) usb_ffs = argv[++i];
        else if (!strcmp(argv[i], "--status-file") && i + 1 < argc) status_file = argv[++i];
        else { usage(argv[0]); return 1; }
    }
    const bool offline = wav_path != NULL;
    if (pace && !offline) { fprintf(stderr, "[pre] --pace requires -i\n"); return 1; }
    if (period_frames == 0 || period_frames > MOZART_RAW_SAMPLES) {
        fprintf(stderr, "[pre] --period-frames 范围 1..%d\n", MOZART_RAW_SAMPLES);
        return 1;
    }
    if (max_frames < 0) max_frames = 0;
    if (usb_ffs && mode_explicit && initial_route == ROUTE_AI) {
        fprintf(stderr, "[pre] USB 输入不支持 --mode ai\n");
        return 1;
    }
    if (usb_ffs) initial_route = ROUTE_DIRECT;
    if (usb_ffs && wav_path) {
        fprintf(stderr, "[pre] --usb-endpoint 与 -i 互斥\n");
        return 1;
    }

    struct stat input_stat;
    if (wav_path && stat(wav_path, &input_stat) < 0) {
        fprintf(stderr, "[pre] status=failed stage=config input_stat path=%s reason=%s\n",
                wav_path, strerror(errno));
        return EXIT_FAILURE;
    }
    if (status_file) {
        struct stat status_stat;
        if (stat(status_file, &status_stat) == 0) {
            if (wav_path && input_stat.st_dev == status_stat.st_dev
                && input_stat.st_ino == status_stat.st_ino) {
                fprintf(stderr, "[pre] status=failed stage=config reason=status_file aliases input\n");
                return EXIT_FAILURE;
            }
        } else if (errno != ENOENT) {
            fprintf(stderr, "[pre] status=failed stage=config status_stat path=%s reason=%s\n",
                    status_file, strerror(errno));
            return EXIT_FAILURE;
        }
    }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    int exit_code = 1;
    int fd = -1, fd2 = -1;
    mozart_dsp_t *dsp = NULL;
    mozart_capture_t *cap = NULL;
    mozart_wav_reader_t *wav = NULL;
    pthread_t play_tid;
    bool play_started = false;
    shared_t shared;
    memset(&shared, 0, sizeof(shared));
    pthread_mutexattr_t mutex_attr;
    int mutex_error = pthread_mutexattr_init(&mutex_attr);
    if (!mutex_error) {
        mutex_error = pthread_mutexattr_setprotocol(&mutex_attr, PTHREAD_PRIO_INHERIT);
        if (!mutex_error) mutex_error = pthread_mutex_init(&shared.status_mutex, &mutex_attr);
        const int attr_error = pthread_mutexattr_destroy(&mutex_attr);
        if (attr_error) {
            fprintf(stderr, "[pre] status=failed stage=status_mutex_attr_destroy reason=%s\n",
                    strerror(attr_error));
            if (!mutex_error) mutex_error = attr_error;
        }
    }
    if (mutex_error) {
        fprintf(stderr, "[pre] status=failed stage=status_mutex_init reason=%s\n",
                strerror(mutex_error));
        return EXIT_FAILURE;
    }
    pthread_t status_tid;
    bool status_started = false;
    status_writer_t writer = { .shared = &shared, .path = status_file };
    shared.usb_input = usb_ffs != NULL;
    atomic_store(&shared.route_req, initial_route);
    atomic_store(&shared.route_cur, initial_route);
    atomic_store(&shared.want_denoise, denoise);
    atomic_store(&shared.want_gate, gate_on);
    atomic_store(&shared.want_agc, agc_on);
    atomic_store(&shared.want_limiter, limiter_on);
    atomic_store(&shared.direct_us_avg, 0.0);

    playback_args_t play_args = { .fd = -1, .shared = &shared,
        .monotonic_pts = !offline || pace };
    // cleanup 可达的全部堆资源：在首个 goto 之前声明并置空。
    mozart_control_t *control = NULL;
    mozart_usblink_t *usblink = NULL;
    int16_t *period_stereo = NULL, *wav_block = NULL;
    float *period_mono = NULL, *trunk = NULL, *staging = NULL;
    // FIFO 同时容纳 WOLA 启动突发和一个未满协商块。
    float usb_fifo[MOZART_RAW_SAMPLES * 2];
    size_t usb_fifo_len = 0;
    uint64_t generation = 0;
    bool finished_generation = false;
    long sent = 0, overruns = 0, send_errors = 0;
    processing_t processing = {0};
    mozart_input_frame_t frame = {0};
    const char *state = "warming", *reason = "udp_open";
    char send_reason[256] = "";

    // ---- UDP 发送端 ----
    fd = udp_open(host, port);
    if (fd < 0) goto cleanup;
    play_args.fd = fd;
    if (second_target) {
        char ip[64] = {0};
        unsigned p = 0;
        if (sscanf(second_target, "%63[^:]:%u", ip, &p) != 2) {
            fprintf(stderr, "[pre] -b 需要格式 IP:端口，得到: %s\n", second_target);
            goto cleanup;
        }
        fd2 = udp_open(ip, (uint16_t)p);
        if (fd2 < 0) goto cleanup;
        fprintf(stderr, "[pre] 双发第二目标: %s:%u\n", ip, p);
    }

    // 显式播放设备必须先就绪，随后才允许采集和发送。
    reason = "playback_open";
    if (play_device) {
        const mozart_playback_config_t pcfg = {
            .device = play_device, .rate = MOZART_OUTPUT_SAMPLE_RATE,
            .channels = 2, .period_frames = period_frames, .n_periods = 4,
            .should_stop = io_should_stop, .stop_context = &play_args,
        };
        play_args.playback = mozart_playback_open(&pcfg);
        if (!play_args.playback) {
            fprintf(stderr, "[play] status=failed stage=open device=%s\n", play_device);
            goto cleanup;
        }
    }
    if (play_device || usb_ffs) {
        reason = "receive_timeout";
        struct timeval tv = { .tv_sec = 0, .tv_usec = 100000 };
        if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv)) < 0) {
            perror("[play] status=failed stage=receive_timeout");
            goto cleanup;
        }
        if (play_device)
            fprintf(stderr, "[play] 播放设备 %s 已打开（period=%u），等待后端输出包...\n",
                    play_device, mozart_playback_period_frames(play_args.playback));
    }

    // ---- 单干 DSP ----
    const mozart_dsp_config_t dcfg = {
        .denoise = denoise, .gate = gate_on, .agc = agc_on, .limiter = limiter_on,
    };
    reason = "dsp_init";
    dsp = mozart_dsp_new(&dcfg);
    if (!dsp) { fprintf(stderr, "[pre] dsp 初始化失败\n"); goto cleanup; }

    // ---- 快流专用端点（M3：绕过守护进程与 aloop）----
    if (usb_ffs) {
        reason = "usb_open";
        usblink = mozart_usblink_open(usb_ffs);
        if (!usblink) goto cleanup;
        fprintf(stderr, "[usb] 快流端点 %s（ep3/ep4）已打开\n", usb_ffs);
    }

    // ---- 控制通道 ----
    update_status(&shared, dsp, usblink, state, "", sent, send_errors, 0, &processing);
    if (ctl_port) {
        const mozart_control_vt vt = {
            .set_route = ctl_set_route, .set_stage = ctl_set_stage,
            .snapshot = ctl_snapshot,
        };
        reason = "control_open";
        control = mozart_control_open(ctl_port, &vt, &shared);
        if (!control) {
            fprintf(stderr, "[pre] status=failed stage=control_open\n");
            goto cleanup;
        }
    }

    // ---- 输入源 ----
    reason = "input_open";
    if (offline) {
        wav = mozart_wav_open(wav_path);
        if (!wav) goto cleanup;
        fprintf(stderr, "[pre] 离线模式: %s → %s:%u%s\n", wav_path, host, port,
                play_device ? " + 播放" : "");
    } else if (usb_ffs) {
        fprintf(stderr, "[pre] USB 端点模式: %s → %s:%u\n", usb_ffs, host, port);
    } else if (!usb_ffs) {
        const mozart_capture_config_t ccfg = {
            .device = device, .rate = MOZART_RAW_SAMPLE_RATE,
            .channels = 2, .period_frames = period_frames, .n_periods = 4,
            .should_stop = io_should_stop, .stop_context = &play_args,
        };
        cap = mozart_capture_open(&ccfg);
        if (!cap) goto cleanup;
        fprintf(stderr, "[pre] 实时模式: %s（period=%u）→ %s:%u%s\n", device, period_frames,
                host, port, play_device ? " + 播放" : "");
    }

    if (play_device || usblink) {
        reason = "reply_thread";
        const int error = pthread_create(&play_tid, NULL, playback_thread, &play_args);
        if (error) {
            fprintf(stderr, "[play] status=failed stage=thread reason=%s\n", strerror(error));
            goto cleanup;
        }
        play_started = true;
    }

    // ---- 主循环资源 ----
    // USB 接收容量固定为最大块长；处理长度取每次 read 的实际返回值。
    const size_t input_cap = usblink ? MOZART_USB_FAST_SAMPLES : period_frames;
    const size_t trunk_cap = (size_t)MOZART_RAW_SAMPLES + input_cap + 512;
    reason = "allocate_buffers";
    period_stereo = malloc(sizeof(int16_t) * input_cap * 2);
    wav_block = malloc(sizeof(int16_t) * MOZART_RAW_SAMPLES * 2);
    period_mono = malloc(sizeof(float) * input_cap);
    trunk = malloc(sizeof(float) * trunk_cap);
    staging = malloc(sizeof(float) * MOZART_RAW_SAMPLES * 2);
    size_t staging_len = 0;
    if (!period_stereo || !wav_block || !period_mono || !trunk || !staging) goto cleanup;

    unsigned char pkt[4 + sizeof(frame)];
    pkt[0] = 0x54; pkt[1] = 0x52; pkt[2] = 0x5A; pkt[3] = 0x4D; // 'MZRT' LE

    uint64_t t0 = mozart_now_ns();
    fade_t fade;
    fade_init(&fade, fade_ms * 48, initial_route == ROUTE_DIRECT);
    bool wav_pending = false;
    size_t wav_pending_len = 0, wav_pos = 0;
    exit_code = 0;
    reason = "";

    if (status_file) {
        // 音频主线程可使用 FIFO 40；磁盘写线程使用普通调度。
        pthread_attr_t attr;
        int error = pthread_attr_init(&attr);
        if (error) {
            status_writer_fail(&shared, "status_thread_attr", error);
            goto cleanup;
        }
        error = pthread_attr_setinheritsched(&attr, PTHREAD_EXPLICIT_SCHED);
        if (!error) error = pthread_attr_setschedpolicy(&attr, SCHED_OTHER);
        const struct sched_param scheduling = { .sched_priority = 0 };
        if (!error) error = pthread_attr_setschedparam(&attr, &scheduling);
        if (!error) {
            error = pthread_create(&status_tid, &attr, status_writer_thread, &writer);
            status_started = error == 0;
        }
        const int destroy_error = pthread_attr_destroy(&attr);
        if (error || destroy_error) {
            if (status_started) {
                atomic_store(&writer.stop, true);
                join_or_exit(status_tid, "status_join");
                status_started = false;
            }
            if (!atomic_load(&shared.status_error))
                status_writer_fail(&shared, "status_thread", error ? error : destroy_error);
            else fprintf(stderr, "[pre] status=failed stage=status_thread reason=%s\n",
                         strerror(error ? error : destroy_error));
            goto cleanup;
        }
    }

    while (!io_should_stop(&play_args) && (max_frames == 0 || sent < max_frames)) {
        size_t got = 0;                       // 本轮取得的立体声帧数
        bool stopping = false;
        const long sent_before = sent;
        if (offline) {
            int n;
            if (!wav_pending) {
                n = mozart_wav_read(wav, wav_block, MOZART_RAW_SAMPLES);
                if (n < MOZART_RAW_SAMPLES) {
                    mozart_wav_rewind(wav);
                    const int m = mozart_wav_read(wav, wav_block + n * 2,
                                                  MOZART_RAW_SAMPLES - n);
                    if (m <= 0) break;
                    n += m;
                }
                wav_pending = true;
                wav_pending_len = (size_t)n;
                wav_pos = 0;
            }
            got = wav_pending_len - wav_pos < period_frames
                      ? wav_pending_len - wav_pos : period_frames;
            memcpy(period_stereo, wav_block + wav_pos * 2, sizeof(int16_t) * got * 2);
            wav_pos += got;
            if (wav_pos >= wav_pending_len) wav_pending = false;
        } else if (usblink) {
            const int error = mozart_usblink_read_audio(usblink, period_stereo,
                                                        MOZART_USB_FAST_SAMPLES);
            if (error < 0) {
                fprintf(stderr, "[pre] status=failed stage=usb_read\n");
                reason = mozart_usblink_last_error(usblink);
                exit_code = 1;
                break;
            }
            got = (size_t)error;
            // read 可以同时更新会话并返回首块音频，必须先重置再增强。
            const uint64_t current = mozart_usblink_generation(usblink);
            if (current != generation) {
                generation = current;
                finished_generation = false;
                mozart_dsp_reset(dsp);
                staging_len = usb_fifo_len = 0;
                memset(&frame, 0, sizeof(frame));
                memset(&processing, 0, sizeof(processing));
                state = "warming";
                fade_init(&fade, fade_ms * 48, true);
            }
            stopping = mozart_usblink_stop_requested(usblink) && !finished_generation;
        } else {
            const int error = mozart_capture_read(cap, period_stereo);
            if (error < 0) {
                if (error != -ECANCELED || !io_should_stop(&play_args)) {
                    fprintf(stderr, "[pre] status=failed stage=capture code=%d\n", error);
                    reason = "capture_read";
                    exit_code = 1;
                }
                break;
            }
            got = period_frames;
        }

        if (atomic_load(&shared.status_error)) break;
        mozart_dsp_set_stages(dsp,
                              atomic_load(&shared.want_denoise),
                              atomic_load(&shared.want_gate),
                              atomic_load(&shared.want_agc),
                              atomic_load(&shared.want_limiter));
        if (usblink && !got && !stopping) goto snapshot;

        // 路由状态机：请求与当前不一致时进入淡变
        const int req = atomic_load(&shared.route_req);
        int cur = atomic_load(&shared.route_cur);
        if (req == ROUTE_DIRECT && cur == ROUTE_AI) {
            atomic_store(&shared.route_cur, ROUTE_DIRECT);
            fade_begin(&fade, +1);
            cur = ROUTE_DIRECT;
        } else if (req == ROUTE_AI && cur == ROUTE_DIRECT && !fade.direction) {
            fade_begin(&fade, -1);
        }

        // 单干增强：FL → 高通 → WOLA → 整形。
        for (size_t i = 0; i < got; i++)
            period_mono[i] = period_stereo[2 * i] / 32768.0f;
        size_t produced = 0;
        if (got) {
            const uint64_t begin = mozart_now_ns();
            produced = mozart_dsp_enhance(dsp, period_mono, got, trunk, trunk_cap);
            processing.last = (long long)((mozart_now_ns() - begin) / 1000);
            if (produced == (size_t)-1) {
                reason = "enhance";
                exit_code = 1;
                break;
            }
            if (processing.last > processing.max) processing.max = processing.last;
            processing.avg = processing.avg * 0.9 + processing.last * 0.1;
        }

        if (usblink) {
            const size_t block = (size_t)mozart_usblink_block_frames(usblink);
            const size_t owed = mozart_usblink_pending_frames(usblink) * block;
            if (stopping && owed > usb_fifo_len) {
                memset(period_mono, 0, block * sizeof(float));
                // 先补一个块完成末窗。短会话在同一次排空中补足初始化窗。
                for (size_t padded = 0;
                     produced + usb_fifo_len < owed && padded < MOZART_RAW_SAMPLES;
                     padded += block) {
                    const uint64_t begin = mozart_now_ns();
                    const size_t n = mozart_dsp_enhance(dsp, period_mono, block,
                                                       trunk + produced, trunk_cap - produced);
                    processing.last = (long long)((mozart_now_ns() - begin) / 1000);
                    if (n == (size_t)-1) {
                        reason = "stop_enhance";
                        exit_code = 1;
                        break;
                    }
                    if (processing.last > processing.max) processing.max = processing.last;
                    processing.avg = processing.avg * 0.9 + processing.last * 0.1;
                    produced += n;
                }
                if (exit_code) break;
                // 仅丢弃补零产生的尾部，原始输入仍进入直出及 20 ms 支路。
                if (produced > owed - usb_fifo_len) produced = owed - usb_fifo_len;
            }
            memcpy(usb_fifo + usb_fifo_len, trunk, produced * sizeof(float));
            usb_fifo_len += produced;
            while (usb_fifo_len >= block && mozart_usblink_pending_frames(usblink)) {
                int16_t direct[MOZART_USB_FAST_SAMPLES * MOZART_USB_CHANNELS];
                for (size_t i = 0; i < block; i++) {
                    float v = usb_fifo[i];
                    if (v > 1.0f) v = 1.0f;
                    else if (v < -1.0f) v = -1.0f;
                    direct[2 * i] = direct[2 * i + 1] = (int16_t)(v * 32767.0f);
                }
                const uint64_t probe0 = mozart_now_ns();
                const int werror = mozart_usblink_write_audio(usblink, direct, (int)block);
                const uint64_t probe1 = mozart_now_ns();
                if (werror < 0) {
                    reason = mozart_usblink_last_error(usblink);
                    fprintf(stderr, "[play] status=failed stage=usb_direct\n");
                    exit_code = 1;
                    break;
                }
                atomic_fetch_add(&shared.direct_periods, 1);
                const long long us = (long long)((probe1 - probe0) / 1000);
                atomic_store(&shared.direct_us_last, us);
                if (us > atomic_load(&shared.direct_us_max))
                    atomic_store(&shared.direct_us_max, us);
                atomic_store(&shared.direct_us_avg,
                             atomic_load(&shared.direct_us_avg) * 0.9 + (double)us * 0.1);
                usb_fifo_len -= block;
                memmove(usb_fifo, usb_fifo + block, usb_fifo_len * sizeof(float));
            }
            if (exit_code) break;
        }
        // 直出（ALSA 模式）：主干 → S16 立体声 → 淡变 → 写设备（≤960 分块）
        else if (play_device && cur == ROUTE_DIRECT && produced > 0) {
            int16_t direct[MOZART_OUTPUT_SAMPLES * 2];
            size_t done = 0;
            while (done < produced) {
                const size_t count = produced - done < MOZART_OUTPUT_SAMPLES
                                         ? produced - done : MOZART_OUTPUT_SAMPLES;
                for (size_t i = 0; i < count; i++) {
                    float v = trunk[done + i];
                    if (v > 1.0f) v = 1.0f;
                    else if (v < -1.0f) v = -1.0f;
                    direct[2 * i] = direct[2 * i + 1] = (int16_t)(v * 32767.0f);
                }
                if (!fade_apply(&fade, direct, count) && req == ROUTE_AI) {
                    atomic_store(&shared.route_cur, ROUTE_AI);   // 淡出完成
                }
                const uint64_t probe0 = mozart_now_ns();
                const int werror = mozart_playback_write_s16_direct(play_args.playback,
                                                                    direct, count);
                const uint64_t probe1 = mozart_now_ns();
                if (werror < 0 && werror != -ECANCELED) {
                    fprintf(stderr, "[play] status=failed stage=direct code=%d\n", werror);
                    reason = "direct_write";
                    atomic_store(&play_args.error, werror);
                    exit_code = 1;
                    break;
                }
                atomic_fetch_add(&shared.direct_periods, 1);
                const long long us = (long long)((probe1 - probe0) / 1000);
                atomic_store(&shared.direct_us_last, us);
                if (us > atomic_load(&shared.direct_us_max))
                    atomic_store(&shared.direct_us_max, us);
                atomic_store(&shared.direct_us_avg,
                             atomic_load(&shared.direct_us_avg) * 0.9 + (double)us * 0.1);
                done += count;
            }
            if (exit_code) break;
        }

        // AI 路径：主干按 960 攒帧 → 打包发送（恒定进行）
        size_t offset = 0;
        while (offset < produced) {
            const size_t take = staging_len + (produced - offset) >= MOZART_RAW_SAMPLES
                                    ? MOZART_RAW_SAMPLES - staging_len
                                    : produced - offset;
            memcpy(staging + staging_len, trunk + offset, sizeof(float) * take);
            staging_len += take;
            offset += take;
            if (staging_len == MOZART_RAW_SAMPLES) {
                if (offline) {
                    frame.meta.pts_ns = (uint64_t)sent * 20000000ull;
                    if (pace) {
                        frame.meta.pts_ns += t0;
                        const uint64_t deadline = frame.meta.pts_ns + 20000000ull;
                        const struct timespec wake = {
                            .tv_sec = (time_t)(deadline / 1000000000ull),
                            .tv_nsec = (long)(deadline % 1000000000ull),
                        };
                        int error;
                        do {
                            error = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &wake, NULL);
                        } while (error == EINTR && !g_stop);
                        if (io_should_stop(&play_args)) break;
                        if (error) {
                            fprintf(stderr, "[pre] replay clock: %s\n", strerror(error));
                            reason = "replay_clock";
                            exit_code = 1;
                            break;
                        }
                    }
                } else {
                    frame.meta.pts_ns = mozart_now_ns() - 20000000ull;
                }
                if (cap) overruns = mozart_capture_overruns(cap);
                if (mozart_dsp_pack_frame(dsp, staging, &frame) < 0) {
                    fprintf(stderr, "[pre] 打包失败\n");
                    reason = "pack_frame";
                    exit_code = 1;
                    break;
                }
                memcpy(pkt + 4, &frame, sizeof(frame));
                bool sent_ok = send(fd, pkt, sizeof(pkt), 0) == (ssize_t)sizeof(pkt);
                if (!sent_ok)
                    snprintf(send_reason, sizeof(send_reason), "mzrt_send primary: %s", strerror(errno));
                if (fd2 >= 0 && send(fd2, pkt, sizeof(pkt), 0) != (ssize_t)sizeof(pkt)) {
                    snprintf(send_reason, sizeof(send_reason), "mzrt_send secondary: %s", strerror(errno));
                    sent_ok = false;
                }
                if (!sent_ok) {
                    reason = send_reason;
                    if (++send_errors % 250 == 1)
                        perror("[pre] send（继续运行）");
                }
                sent++;
                staging_len = 0;
            }
        }

        if (exit_code) break;
        if (produced) state = "ready";
        if (stopping) {
            if (mozart_usblink_finish(usblink) < 0) {
                reason = mozart_usblink_last_error(usblink);
                exit_code = 1;
                break;
            }
            finished_generation = true;
            usb_fifo_len = 0;
        }

snapshot:
        // 空闲 read 的最长等待为 100 ms，状态和控制仍持续更新。
        update_status(&shared, dsp, usblink, state, reason, sent, send_errors,
                      frame.meta.conf, &processing);

        if (sent != sent_before && sent % 250 == 0 && sent > 0) {
            fprintf(stderr, "[pre] frames=%ld vad=%u seg=%u energy=%u dB overruns=%ld "
                            "mode=%s elapsed=%.1fs\n",
                    sent, frame.meta.vad_flag, frame.meta.segment_id,
                    frame.meta.energy_db, overruns,
                    atomic_load(&shared.route_cur) == ROUTE_DIRECT ? "direct" : "ai",
                    (mozart_now_ns() - t0) / 1e9);
        }
    }

    fprintf(stderr, "[pre] 结束: 发送 %ld 帧 (%.1f s), overruns=%ld\n",
            sent, sent * 0.02, overruns);

    // 有限输入等待最后一帧回包。到期后报告排空失败并回收资源。
    if (play_started && !io_should_stop(&play_args) && exit_code == 0) {
        const uint64_t deadline = mozart_now_ns() + 2000000000ull;
        const struct timespec poll = { .tv_sec = 0, .tv_nsec = 10000000 };
        while (atomic_load(&play_args.last_played_frame) < (uint32_t)sent
               && mozart_now_ns() < deadline && !io_should_stop(&play_args)) {
            nanosleep(&poll, NULL);
        }
        if (!io_should_stop(&play_args)
            && atomic_load(&play_args.last_played_frame) < (uint32_t)sent
            && atomic_load(&shared.route_cur) == ROUTE_AI) {
            reason = "reply_drain";
            fprintf(stderr, "[play] status=failed stage=drain last=%u expected=%ld\n",
                    atomic_load(&play_args.last_played_frame), sent);
            exit_code = 1;
        }
        const struct timespec tail = { .tv_sec = 0, .tv_nsec = 100000000 };
        if (!g_stop) nanosleep(&tail, NULL);
    }
cleanup:
    atomic_store(&writer.stop, true);
    if (status_started) join_or_exit(status_tid, "status_join");
    atomic_store(&play_args.stop, true);
    if (play_started) {
        shutdown(fd, SHUT_RDWR);
        join_or_exit(play_tid, "reply_join");
    }

    if (atomic_load(&shared.status_error)) {
        if (!exit_code) reason = shared.status_error_reason;
        exit_code = 1;
    }
    if (atomic_load(&play_args.error) != 0) {
        if (!exit_code) reason = "backend_reply_io";
        exit_code = 1;
    }
    char close_reason[256];
    const int sockets[] = { fd2, fd };
    for (size_t i = 0; i < sizeof(sockets) / sizeof(sockets[0]); i++) {
        if (sockets[i] >= 0 && close(sockets[i]) < 0) {
            const int error = errno;
            fprintf(stderr, "[pre] status=failed stage=socket_close errno=%d reason=%s\n",
                    error, strerror(error));
            if (!exit_code) {
                snprintf(close_reason, sizeof(close_reason), "socket_close: %s", strerror(error));
                reason = close_reason;
            }
            exit_code = 1;
        }
    }
    update_status(&shared, dsp, usblink, exit_code ? "failed" : "stopped",
                  reason, sent, send_errors, frame.meta.conf, &processing);
    if (status_file) {
        char snapshot[sizeof(shared.status)];
        ctl_snapshot(&shared, snapshot, sizeof(snapshot));
        const int error = persist_status(status_file, snapshot);
        if (error) {
            // 保留先前的失败原因；最终写入错误仍单独输出到日志。
            if (!atomic_load(&shared.status_error)) status_writer_fail(&shared, "status_file", error);
            else fprintf(stderr, "[pre] final status=failed stage=status_file errno=%d reason=%s\n",
                         error, strerror(error));
            if (!exit_code) reason = shared.status_error_reason;
            exit_code = 1;
            update_status(&shared, dsp, usblink, "failed", reason,
                          sent, send_errors, frame.meta.conf, &processing);
        }
    }
    mozart_control_close(control);
    mozart_usblink_close(usblink);
    mozart_playback_close(play_args.playback);
    mozart_capture_close(cap);
    mozart_wav_close(wav);
    mozart_dsp_free(dsp);
    free(period_stereo);
    free(wav_block);
    free(period_mono);
    free(trunk);
    free(staging);
    const int destroy_error = pthread_mutex_destroy(&shared.status_mutex);
    if (destroy_error) {
        fprintf(stderr, "[pre] status=failed stage=status_mutex_destroy reason=%s\n",
                strerror(destroy_error));
        exit_code = 1;
    }
    return exit_code;
}
