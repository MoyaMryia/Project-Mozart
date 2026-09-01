// main.c — mozart-pre：USB 麦克风采集 → DSP 链 → MZRT UDP 发送/接收
// ============================================================================
// 实时模式：
//   mozart-pre [-d <alsa 采集设备>] [-o <alsa 播放设备>] [-a <目标 IP>]
//              [-p <目标端口>] [-b <第二目标IP:端口>] [-n <帧数>]
//              [--no-rnnoise] [--model <path.rnnoise>]
// 离线模式（无麦克风验证链路，输入须 48k/立体声/PCM16 WAV）：
//   mozart-pre -i <input.wav> [其余参数同上，-n 默认无限循环]
//
// 输出：MZRT 输入契约包（20B 头 + 320×4B PCM = 1300B）逐帧 UDP 发送。
// 输入：当指定 -o 时，同 UDP 端口接收后端 MZRT 输出契约包（3860B）并 ALSA 播放。
// -b 第二目标（如 STT 服务）：同一帧双发；发送失败不致命（目标离线继续跑）。
#define _POSIX_C_SOURCE 200809L
#include "mozart.h"
#include "mozart/playback.h"

#include <arpa/inet.h>
#include <errno.h>
#include <netdb.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

static volatile sig_atomic_t g_stop = 0;

static void on_signal(int sig) { (void)sig; g_stop = 1; }

static void usage(const char *prog)
{
    fprintf(stderr,
        "用法: %s [-d alsa采集设备] [-o alsa播放设备] [-a 目标IP] [-p 端口] [-n 帧数] "
        "[-i 输入.wav] [--no-rnnoise] [--model x.rnnoise]\n"
        "  -d  ALSA 采集设备（默认 default；麦克风为 hw:X,0，S16_LE/2ch/48k）\n"
        "  -o  ALSA 播放设备（如 plughw:X,0；缺省则不播放，只发送）\n"
        "  -a  rvc-backend 地址（默认 127.0.0.1）\n"
        "  -p  rvc-backend 音频端口（默认 18000）\n"
        "  -n  发送帧数上限，0 = 无限（默认实时 0 / 离线循环播放）\n"
        "  -i  离线模式：读 48k/立体声/PCM16 WAV 代替麦克风\n",
        prog);
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

// ---- 播放线程参数 ----------------------------------------------------------
typedef struct {
    int                 fd;         // 已连接的后端 UDP socket（收发同口）
    const char         *device;     // ALSA 播放设备
    volatile sig_atomic_t *stop;    // 共享停止标志
} playback_args_t;

// ---- 播放线程：收 MZRT 输出包 → ALSA ---------------------------------------
static void *playback_thread(void *arg)
{
    playback_args_t *pa = (playback_args_t *)arg;
    mozart_playback_config_t pcfg = {
        .device = pa->device, .rate = MOZART_OUTPUT_SAMPLE_RATE,
        .channels = 2, .period_frames = MOZART_OUTPUT_SAMPLES, .n_periods = 4,
    };
    mozart_playback_t *pb = mozart_playback_open(&pcfg);
    if (!pb) {
        fprintf(stderr, "[play] 无法打开播放设备 %s，仅发送模式\n", pa->device);
        return NULL;
    }
    fprintf(stderr, "[play] 播放设备 %s 已打开，等待后端输出包...\n", pa->device);

    // 100ms recv 超时，便于检查停止标志
    struct timeval tv = { .tv_sec = 0, .tv_usec = 100000 };
    setsockopt(pa->fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    unsigned char pkt[4 + sizeof(mozart_output_frame_t)];
    long received = 0, played = 0, bad_packets = 0;
    double latency_sum_ms = 0.0, latency_max_ms = 0.0;
    long latency_count = 0;
    uint64_t t0 = mozart_now_ns();

    while (!*pa->stop) {
        ssize_t n = recv(pa->fd, pkt, sizeof(pkt), 0);
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) continue;
            if (!*pa->stop) perror("[play] recv");
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

        if (mozart_playback_write(pb, frame.pcm, MOZART_OUTPUT_SAMPLES) == 0) {
            played++;
        }

        // 端到端延迟：pts 可信（>1s）才统计
        if (frame.meta.pts_ns > 1000000000ull) {
            const uint64_t now = mozart_now_ns();
            const double lat_ms = (double)(now - frame.meta.pts_ns) / 1e6;
            latency_sum_ms += lat_ms;
            if (lat_ms > latency_max_ms) latency_max_ms = lat_ms;
            latency_count++;
        }

        if (played % 250 == 0 && played > 0) {   // 每 5s 一条心跳
            fprintf(stderr,
                "[play] played=%ld/%ld bad=%ld underruns=%ld e2e_avg=%.1fms "
                "e2e_max=%.1fms elapsed=%.1fs\n",
                played, received, bad_packets,
                mozart_playback_underruns(pb),
                latency_count ? latency_sum_ms / latency_count : 0.0,
                latency_max_ms,
                (mozart_now_ns() - t0) / 1e9);
        }
    }

    fprintf(stderr,
        "[play] 结束: 播放 %ld/%ld 包, bad=%ld, underruns=%ld, e2e_avg=%.1fms, e2e_max=%.1fms\n",
        played, received, bad_packets, mozart_playback_underruns(pb),
        latency_count ? latency_sum_ms / latency_count : 0.0, latency_max_ms);
    mozart_playback_close(pb);
    return NULL;
}

int main(int argc, char **argv)
{
    const char *device = "default";
    const char *play_device = NULL;
    const char *host = "127.0.0.1";
    const char *second_target = NULL;
    const char *wav_path = NULL;
    const char *rn_model = NULL;
    bool use_rnnoise = true;
    uint16_t port = 18000;
    long max_frames = -1;   // -1 = 按模式默认

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-d") && i + 1 < argc) device = argv[++i];
        else if (!strcmp(argv[i], "-o") && i + 1 < argc) play_device = argv[++i];
        else if (!strcmp(argv[i], "-a") && i + 1 < argc) host = argv[++i];
        else if (!strcmp(argv[i], "-p") && i + 1 < argc) port = (uint16_t)atoi(argv[++i]);
        else if (!strcmp(argv[i], "-b") && i + 1 < argc) second_target = argv[++i];
        else if (!strcmp(argv[i], "-n") && i + 1 < argc) max_frames = atol(argv[++i]);
        else if (!strcmp(argv[i], "-i") && i + 1 < argc) wav_path = argv[++i];
        else if (!strcmp(argv[i], "--no-rnnoise")) use_rnnoise = false;
        else if (!strcmp(argv[i], "--model") && i + 1 < argc) rn_model = argv[++i];
        else { usage(argv[0]); return 1; }
    }
    bool offline = wav_path != NULL;
    if (max_frames < 0) max_frames = 0; // 两种模式都无限，-n>0 截断，SIGINT 退出

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    // ---- UDP 发送端 ----
    int fd = udp_open(host, port);
    if (fd < 0) return 1;
    int fd2 = -1;
    if (second_target) {
        char ip[64] = {0};
        unsigned p = 0;
        if (sscanf(second_target, "%63[^:]:%u", ip, &p) != 2) {
            fprintf(stderr, "[pre] -b 需要格式 IP:端口，得到: %s\n", second_target);
            return 1;
        }
        fd2 = udp_open(ip, (uint16_t)p);
        if (fd2 < 0) return 1;
        fprintf(stderr, "[pre] 双发第二目标: %s:%u\n", ip, p);
    }

    // ---- 播放线程（可选）----
    pthread_t play_tid = 0;
    playback_args_t play_args = { .fd = fd, .device = play_device, .stop = &g_stop };
    if (play_device) {
        if (pthread_create(&play_tid, NULL, playback_thread, &play_args) != 0) {
            fprintf(stderr, "[pre] 播放线程创建失败，仅发送模式\n");
            play_tid = 0;
        }
    }

    // ---- DSP 链 ----
    mozart_dsp_config_t dcfg = { .rnnoise = use_rnnoise, .rnnoise_model = rn_model };
    mozart_dsp_t *dsp = mozart_dsp_new(&dcfg);
    if (!dsp) { fprintf(stderr, "dsp 初始化失败\n"); return 1; }

    // ---- 输入源 ----
    mozart_capture_t *cap = NULL;
    mozart_wav_reader_t *wav = NULL;
    if (offline) {
        wav = mozart_wav_open(wav_path);
        if (!wav) return 1;
        fprintf(stderr, "[pre] 离线模式: %s → %s:%u%s\n", wav_path, host, port,
                play_device ? " + 播放" : "");
    } else {
        mozart_capture_config_t ccfg = {
            .device = device, .rate = MOZART_RAW_SAMPLE_RATE,
            .channels = 2, .period_frames = MOZART_RAW_SAMPLES, .n_periods = 4,
        };
        cap = mozart_capture_open(&ccfg);
        if (!cap) return 1;
        fprintf(stderr, "[pre] 实时模式: %s → %s:%u%s\n", device, host, port,
                play_device ? " + 播放" : "");
    }

    // ---- 主循环 ----
    int16_t stereo[MOZART_RAW_SAMPLES * 2];
    mozart_input_frame_t frame;
    // MZRT 输入包：4B magic(LE) + 16B meta + 320×4B PCM = 1300B
    unsigned char pkt[4 + sizeof(frame)];
    pkt[0] = 0x54; pkt[1] = 0x52; pkt[2] = 0x5A; pkt[3] = 0x4D; // 'MZRT' LE
    long sent = 0, overruns = 0, send_errors = 0;
    uint64_t t0 = mozart_now_ns();

    while (!g_stop && (max_frames == 0 || sent < max_frames)) {
        int n;
        if (offline) {
            n = mozart_wav_read(wav, stereo, MOZART_RAW_SAMPLES);
            if (n < MOZART_RAW_SAMPLES) {          // 循环播放
                mozart_wav_rewind(wav);
                int m = mozart_wav_read(wav, stereo + n * 2,
                                        MOZART_RAW_SAMPLES - n);
                if (m <= 0) break;
                n += m;
            }
            frame.meta.pts_ns = (uint64_t)sent * 20000000ull; // 20ms/帧
        } else {
            if (mozart_capture_read(cap, stereo) < 0) {
                fprintf(stderr, "[pre] 采集失败，退出\n");
                break;
            }
            // 本帧覆盖 [now-20ms, now]，pts 取帧起始
            frame.meta.pts_ns = mozart_now_ns() - 20000000ull;
        }
        if (cap) overruns = mozart_capture_overruns(cap);

        if (mozart_dsp_process(dsp, stereo, &frame) < 0) {
            fprintf(stderr, "[pre] dsp 处理失败\n");
            break;
        }
        memcpy(pkt + 4, &frame, sizeof(frame));
        bool sent_ok = send(fd, pkt, sizeof(pkt), 0) == (ssize_t)sizeof(pkt);
        if (fd2 >= 0 && send(fd2, pkt, sizeof(pkt), 0) != (ssize_t)sizeof(pkt))
            sent_ok = false;
        if (!sent_ok) {
            // 目标离线不致命（双发场景常见）：计数并继续，每 250 帧提醒一次
            if (++send_errors % 250 == 1)
                perror("[pre] send（继续运行）");
        }
        sent++;

        if (sent % 250 == 0) {   // 每 5s 一条心跳
            fprintf(stderr, "[pre] frames=%ld vad=%u seg=%u energy=%u dB "
                            "overruns=%ld elapsed=%.1fs\n",
                    sent, frame.meta.vad_flag, frame.meta.segment_id,
                    frame.meta.energy_db, overruns,
                    (mozart_now_ns() - t0) / 1e9);
        }
    }

    fprintf(stderr, "[pre] 结束: 发送 %ld 帧 (%.1f s), overruns=%ld\n",
            sent, sent * 0.02, overruns);

    // 通知播放线程退出并等待
    g_stop = 1;
    if (play_tid) {
        // 唤醒可能阻塞在 recv 的线程
        shutdown(fd, SHUT_RDWR);
        pthread_join(play_tid, NULL);
    }

    mozart_capture_close(cap);
    mozart_wav_close(wav);
    mozart_dsp_free(dsp);
    close(fd);
    return 0;
}
