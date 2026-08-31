#!/usr/bin/env python3
# tts2rvc.py — TTS → RVC 变声 → 播放 的演示闭环
# ============================================================================
# 把 Matcha TTS 合成的语音重采样成 16k 契约帧，伪装成"麦克风"按 20ms 节奏
# 打 MZRT 包喂给 rvc-backend（RT_RVC 滑窗流式），并在同一 socket 上收变声
# 后的 48k 回包实时播放——文字进，变声出。
#
# 前置：
#   1. rvc-backend 运行且 rt_rvc 已激活（无模型时后端回退 3x 直通，
#      听感=原声；装好模型后即真变声）
#   2. mozart-pre 未占用该后端（第一个发包的客户端收回包）
#
# 用法：
#   echo "你好，我是AI生成的声音，现在正在被实时变声。" | \
#     .venv/bin/python tools/tts2rvc.py
import argparse
import os
import socket
import struct
import subprocess
import sys
import threading
import time

import numpy as np
import sherpa_onnx
import soundfile as sf

MZRT_MAGIC = 0x4D5A5254
HEADER = struct.Struct("<IQIBBBB")
IN_SAMPLES = 320          # 20ms @16k
IN_RATE = 16000
OUT_SAMPLES = 960         # 20ms @48k
OUT_RATE = 48000


def build_tts(model, threads=4):
    return sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            matcha=sherpa_onnx.OfflineTtsMatchaModelConfig(
                acoustic_model=f"{model}/model-steps-3.onnx",
                vocoder=f"{model}/hifigan_v2.onnx",
                lexicon=f"{model}/lexicon.txt",
                tokens=f"{model}/tokens.txt",
                data_dir=f"{model}/dict",
                dict_dir=f"{model}/dict",
                noise_scale=0.667,
                length_scale=1.0),
            num_threads=threads),
        rule_fsts=f"{model}/date.fst,{model}/number.fst"))


def resample_linear(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return x
    n_out = int(len(x) * dst / src)
    pos = np.linspace(0, len(x) - 1, n_out)
    i0 = pos.astype(np.int64)
    i1 = np.minimum(i0 + 1, len(x) - 1)
    frac = pos - i0
    return x[i0] * (1 - frac) + x[i1] * frac


def main():
    ap = argparse.ArgumentParser(description="TTS -> RVC voice changer demo")
    ap.add_argument("--backend", default="127.0.0.1:18000")
    ap.add_argument("--tts-model", default=os.path.expanduser(
        "~/models/sherpa-onnx/matcha-zh-baker"))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=0, help="本机接收端口（0=随机）")
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()

    b_host, b_port = args.backend.rsplit(":", 1)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.host, args.port))
    sock.settimeout(0.05)
    sock.connect((b_host, int(b_port)))
    local_port = sock.getsockname()[1]
    print(f"[tts2rvc] local :{local_port} -> {args.backend}", flush=True)

    # ---- 回包播放线程：MZRT 输出包(3860B) → aplay 原始 f32le 48k mono ----
    aplay = subprocess.Popen(
        ["aplay", "-q", "-t", "raw", "-f", "FLOAT_LE", "-r", str(OUT_RATE),
         "-c", "1", "-D", "plughw:1,3"],
        stdin=subprocess.PIPE)
    out_packets = 0

    def recv_loop():
        nonlocal out_packets
        while True:
            try:
                pkt, _ = sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(pkt) != HEADER.size + OUT_SAMPLES * 4:
                continue
            try:
                aplay.stdin.write(pkt[HEADER.size:])
                aplay.stdin.flush()
                out_packets += 1
            except BrokenPipeError:
                return

    threading.Thread(target=recv_loop, daemon=True).start()

    tts = build_tts(args.tts_model, args.threads)
    print(f"[tts2rvc] tts ready (sr={tts.sample_rate})", flush=True)

    frame_idx = 0
    pts_ns = time.monotonic_ns()
    sent_total = 0

    def speak(text):
        nonlocal frame_idx, pts_ns, sent_total
        t0 = time.monotonic()
        audio = tts.generate(text, sid=0, speed=1.0)
        samples = np.asarray(audio.samples, dtype=np.float32)
        synth = time.monotonic() - t0
        pcm16k = resample_linear(samples, tts.sample_rate, IN_RATE)
        peak = float(np.max(np.abs(pcm16k))) if len(pcm16k) else 0.0
        if peak > 1.0:
            pcm16k /= peak
        n_frames = len(pcm16k) // IN_SAMPLES
        print(f"[tts2rvc] synth {len(samples)/tts.sample_rate:.1f}s in {synth:.2f}s "
              f"-> {n_frames} contract frames (peak {peak:.2f})", flush=True)

        for k in range(n_frames):
            chunk = pcm16k[k * IN_SAMPLES:(k + 1) * IN_SAMPLES]
            hdr = HEADER.pack(MZRT_MAGIC, pts_ns, frame_idx + 1, 1, 220,
                              255, 1)
            pkt = hdr + chunk.tobytes()
            sock.send(pkt)
            frame_idx += 1
            sent_total += 1
            pts_ns += 20_000_000
            time.sleep(0.02)          # 20ms 实时节拍
        # 收尾补 1s 静音帧，把滑窗尾巴顶出去
        silence = np.zeros(IN_SAMPLES, dtype=np.float32)
        for _ in range(50):
            hdr = HEADER.pack(MZRT_MAGIC, pts_ns, frame_idx + 1, 0, 0, 0, 0)
            sock.send(hdr + silence.tobytes())
            frame_idx += 1
            sent_total += 1
            pts_ns += 20_000_000
            time.sleep(0.02)

    print("[tts2rvc] 输入句子，回车即合成并变声播放；Ctrl-C 退出", flush=True)
    try:
        for line in sys.stdin:
            text = line.strip()
            if text:
                speak(text)
                time.sleep(1.0)       # 留出滑窗/推理时间
                print(f"[tts2rvc] 回包 {out_packets} 块", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        print(f"[tts2rvc] exit: sent={sent_total} out={out_packets}", flush=True)


if __name__ == "__main__":
    main()
