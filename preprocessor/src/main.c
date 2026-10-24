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
    char status[1024];
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
    return g_stop || atomic_load(&pa->stop) || atomic_load(&pa->error) != 0;
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

static void ctl_set_route(void *ctx, int route)
{
    shared_t *shared = ctx;
    atomic_store(&shared->route_req, route ? ROUTE_DIRECT : ROUTE_AI);
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
    pthread_mutex_lock(&shared->status_mutex);
    snprintf(buf, size, "%s", shared->status);
    pthread_mutex_unlock(&shared->status_mutex);
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
        "[--no-denoise] [--gate] [--agc] [--limiter]\n"
        "  -d   ALSA 采集设备（默认 default）\n"
        "  -o   ALSA 播放设备；缺省只发送不播放\n"
        "  --period-frames  采集/直出周期（默认 128，≤960；AI 路径仍按 960 攒帧）\n"
        "  --mode           初始播放路由（默认 ai；direct = 主干直出播放）\n"
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
    bool denoise = true, gate_on = false, agc_on = false, limiter_on = false;
    bool pace = false;
    uint16_t port = 18000;
    uint16_t ctl_port = 19040;
    unsigned period_frames = 128;
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
            if (!strcmp(argv[++i], "direct")) initial_route = ROUTE_DIRECT;
            else if (!strcmp(argv[i], "ai")) initial_route = ROUTE_AI;
            else { usage(argv[0]); return 1; }
        }
        else if (!strcmp(argv[i], "--fade-ms") && i + 1 < argc) fade_ms = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--usb-endpoint") && i + 1 < argc) usb_ffs = argv[++i];
        else { usage(argv[0]); return 1; }
    }
    const bool offline = wav_path != NULL;
    if (pace && !offline) { fprintf(stderr, "[pre] --pace requires -i\n"); return 1; }
    if (period_frames == 0 || period_frames > MOZART_RAW_SAMPLES) {
        fprintf(stderr, "[pre] --period-frames 范围 1..%d\n", MOZART_RAW_SAMPLES);
        return 1;
    }
    if (max_frames < 0) max_frames = 0;
    if (usb_ffs && period_frames != 128) {
        fprintf(stderr, "[pre] --usb-endpoint 要求 --period-frames 128（快流帧长）\n");
        return 1;
    }
    if (usb_ffs && wav_path) {
        fprintf(stderr, "[pre] --usb-endpoint 与 -i 互斥\n");
        return 1;
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
    pthread_mutex_init(&shared.status_mutex, NULL);
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
        struct timeval tv = { .tv_sec = 0, .tv_usec = 100000 };
        if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv)) < 0) {
            perror("[play] status=failed stage=receive_timeout");
            goto cleanup;
        }
        fprintf(stderr, "[play] 播放设备 %s 已打开（period=%u），等待后端输出包...\n",
                play_device, mozart_playback_period_frames(play_args.playback));
    }

    // ---- 单干 DSP ----
    const mozart_dsp_config_t dcfg = {
        .denoise = denoise, .gate = gate_on, .agc = agc_on, .limiter = limiter_on,
    };
    dsp = mozart_dsp_new(&dcfg);
    if (!dsp) { fprintf(stderr, "[pre] dsp 初始化失败\n"); goto cleanup; }

    // ---- 快流专用端点（M3：绕过守护进程与 aloop）----
    if (usb_ffs) {
        usblink = mozart_usblink_open(usb_ffs);
        if (!usblink) goto cleanup;
        fprintf(stderr, "[usb] 快流端点 %s（ep3/ep4）已打开\n", usb_ffs);
    }

    // ---- 控制通道 ----
    if (ctl_port) {
        const mozart_control_vt vt = {
            .set_route = ctl_set_route, .set_stage = ctl_set_stage,
            .snapshot = ctl_snapshot,
        };
        control = mozart_control_open(ctl_port, &vt, &shared);
        // 绑定失败不致命：降级为无控制通道，原因已由 control 模块打印。
    }

    // ---- 输入源 ----
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

    if (play_device) {
        const int error = pthread_create(&play_tid, NULL, playback_thread, &play_args);
        if (error) {
            fprintf(stderr, "[play] status=failed stage=thread reason=%s\n", strerror(error));
            goto cleanup;
        }
        play_started = true;
    }

    // ---- 主循环资源 ----
    // 无预热时首次 enhance 输出不满块，且 set_stages 重建后排空有突发，
    // 容量按最坏情况给足以保索引对齐。
    const size_t trunk_cap = (size_t)MOZART_RAW_SAMPLES + period_frames + 512;
    period_stereo = malloc(sizeof(int16_t) * period_frames * 2);
    wav_block = malloc(sizeof(int16_t) * MOZART_RAW_SAMPLES * 2);
    period_mono = malloc(sizeof(float) * period_frames);
    trunk = malloc(sizeof(float) * trunk_cap);
    staging = malloc(sizeof(float) * MOZART_RAW_SAMPLES * 2);
    size_t staging_len = 0;
    if (!period_stereo || !wav_block || !period_mono || !trunk || !staging) goto cleanup;

    mozart_input_frame_t frame;
    unsigned char pkt[4 + sizeof(frame)];
    pkt[0] = 0x54; pkt[1] = 0x52; pkt[2] = 0x5A; pkt[3] = 0x4D; // 'MZRT' LE

    long sent = 0, overruns = 0, send_errors = 0;
    uint64_t t0 = mozart_now_ns();
    fade_t fade;
    fade_init(&fade, fade_ms * 48, initial_route == ROUTE_DIRECT);
    bool wav_pending = false;
    size_t wav_pending_len = 0, wav_pos = 0;
    exit_code = 0;

    while (!io_should_stop(&play_args) && (max_frames == 0 || sent < max_frames)) {
        size_t got = 0;                       // 本轮取得的立体声帧数
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
                                                        (int)period_frames);
            if (error < 0) {
                fprintf(stderr, "[pre] status=failed stage=usb_read\n");
                exit_code = 1;
                break;
            }
            got = (size_t)error;
        } else {
            const int error = mozart_capture_read(cap, period_stereo);
            if (error < 0) {
                if (error != -ECANCELED || !io_should_stop(&play_args)) {
                    fprintf(stderr, "[pre] status=failed stage=capture code=%d\n", error);
                    exit_code = 1;
                }
                break;
            }
            got = period_frames;
        }

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

        // 单干增强：FL → 高通 → WOLA → 整形
        for (size_t i = 0; i < got; i++)
            period_mono[i] = period_stereo[2 * i] / 32768.0f;
        const size_t produced = mozart_dsp_enhance(dsp, period_mono, got,
                                                   trunk, trunk_cap);

        // 直出（usb 模式）：主干 → S16 立体声 → 淡变 → 快流端点（128 样本帧）
        if (usblink && cur == ROUTE_DIRECT && produced > 0) {
            int16_t direct[MOZART_OUTPUT_SAMPLES * 2];
            size_t done = 0;
            while (done + 128 <= produced) {
                for (int i = 0; i < 128; i++) {
                    float v = trunk[done + i];
                    if (v > 1.0f) v = 1.0f;
                    else if (v < -1.0f) v = -1.0f;
                    direct[2 * i] = direct[2 * i + 1] = (int16_t)(v * 32767.0f);
                }
                fade_apply(&fade, direct, 128);
                if (req == ROUTE_AI && fade.direction == 0)
                    atomic_store(&shared.route_cur, ROUTE_AI);
                const uint64_t probe0 = mozart_now_ns();
                const int werror = mozart_usblink_write_audio(usblink, direct, 128);
                const uint64_t probe1 = mozart_now_ns();
                if (werror < 0) {
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
                done += 128;
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
                    exit_code = 1;
                    break;
                }
                memcpy(pkt + 4, &frame, sizeof(frame));
                bool sent_ok = send(fd, pkt, sizeof(pkt), 0) == (ssize_t)sizeof(pkt);
                if (fd2 >= 0 && send(fd2, pkt, sizeof(pkt), 0) != (ssize_t)sizeof(pkt))
                    sent_ok = false;
                if (!sent_ok) {
                    if (++send_errors % 250 == 1)
                        perror("[pre] send（继续运行）");
                }
                sent++;
                staging_len = 0;
            }
        }

        // 级开关应用与状态快照
        mozart_dsp_set_stages(dsp,
                              atomic_load(&shared.want_denoise),
                              atomic_load(&shared.want_gate),
                              atomic_load(&shared.want_agc),
                              atomic_load(&shared.want_limiter));
        if (sent % 20 == 1) {
            mozart_dsp_telemetry_t tm;
            mozart_dsp_telemetry(dsp, &tm);
            pthread_mutex_lock(&shared.status_mutex);
            snprintf(shared.status, sizeof(shared.status),
                "{\"mode\":\"%s\",\"stages\":{\"denoise\":%d,\"gate\":%d,\"agc\":%d,"
                "\"limiter\":%d},\"frames\":%ld,\"direct_periods\":%lu,"
                "\"skipped_replies\":%lu,\"send_errors\":%ld,"
                "\"direct_us\":{\"last\":%lld,\"max\":%lld,\"avg\":%.0f},"
                "\"ns_gain\":%.3f,\"gate_open\":%ld,\"vad_conf\":%.3f}",
                atomic_load(&shared.route_cur) == ROUTE_DIRECT ? "direct" : "ai",
                tm.denoise, tm.gate, tm.agc, tm.limiter, (long)sent,
                (unsigned long)atomic_load(&shared.direct_periods),
                (unsigned long)atomic_load(&shared.skipped_replies),
                send_errors,
                (long long)atomic_load(&shared.direct_us_last),
                (long long)atomic_load(&shared.direct_us_max),
                (double)atomic_load(&shared.direct_us_avg),
                (double)tm.ns_last_gain, (long)tm.gate_open,
                frame.meta.conf / 255.0);
            pthread_mutex_unlock(&shared.status_mutex);
        }

        if (sent % 250 == 0 && sent > 0) {
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
            fprintf(stderr, "[play] status=failed stage=drain last=%u expected=%ld\n",
                    atomic_load(&play_args.last_played_frame), sent);
            exit_code = 1;
        }
        const struct timespec tail = { .tv_sec = 0, .tv_nsec = 100000000 };
        if (!g_stop) nanosleep(&tail, NULL);
    }
cleanup:
    atomic_store(&play_args.stop, true);
    mozart_control_close(control);
    mozart_usblink_close(usblink);
    if (play_started) {
        shutdown(fd, SHUT_RDWR);
        pthread_join(play_tid, NULL);
    }

    if (atomic_load(&play_args.error) != 0) exit_code = 1;
    mozart_playback_close(play_args.playback);
    mozart_capture_close(cap);
    mozart_wav_close(wav);
    mozart_dsp_free(dsp);
    free(period_stereo);
    free(wav_block);
    free(period_mono);
    free(trunk);
    free(staging);
    pthread_mutex_destroy(&shared.status_mutex);
    if (fd2 >= 0) close(fd2);
    if (fd >= 0) close(fd);
    return exit_code;
}
