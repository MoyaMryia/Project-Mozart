#!/usr/bin/env python3
# bench_concurrent.py — 极限并发实时性基准
# ============================================================================
# 场景：板上同时跑四条算力路径，测各自的实时性退化
#   主测：RVC 滑窗流式（rt_rvc，UDP 实时喂入）→ 输出 RTF + 端到端延迟
#   负载：TTS 合成循环（CPU）+ ASR 解码循环（CPU）+ LLM 翻译循环（GPU）
# 前置：rvc-backend 运行且 rt_rvc active（真模型）；llama-server :18200 常驻
import argparse
import json
import os
import socket
import statistics
import struct
import threading
import time
import urllib.request

import numpy as np
import sherpa_onnx
import soundfile as sf

MZRT_MAGIC = 0x4D5A5254
HEADER = struct.Struct("<IQIBBBB")
IN_SAMPLES, IN_RATE, OUT_SAMPLES, OUT_RATE = 320, 16000, 960, 48000

STOP = threading.Event()


def load_wav_16k(path):
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    mono = data[:, 0]
    if sr != IN_RATE:
        n = int(len(mono) * IN_RATE / sr)
        pos = np.linspace(0, len(mono) - 1, n)
        i0 = pos.astype(np.int64)
        mono = mono[i0] * (1 - (pos - i0)) + mono[np.minimum(i0 + 1, len(mono) - 1)] * (pos - i0)
    return mono.astype(np.float32)


def cpu_total():
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = list(map(int, parts))
    return sum(vals), vals[3]  # total, idle


def cpu_percent_sample(prev):
    total, idle = cpu_total()
    dt, di = total - prev[0], idle - prev[1]
    return 100.0 * (1 - di / dt) if dt else 0.0, (total, idle)


# ---- 负载 1：TTS 循环（CPU）----
def tts_load(model, wav_ref_duration, results):
    tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=f"{model}/model.int8.onnx", lexicon=f"{model}/lexicon.txt",
                tokens=f"{model}/tokens.txt", data_dir=f"{model}/dict",
                dict_dir=f"{model}/dict", noise_scale=0.667, noise_scale_w=0.8),
            num_threads=2)))
    n, rtf = 0, []
    sentences = ["这是一段用来压测的语音合成负载，句子稍微长一点才有意义。",
                 "The jetson orin nano is running four model paths at once.",
                 "极限并发测试，看看实时性还剩多少。"]
    while not STOP.is_set():
        t0 = time.monotonic()
        a = tts.generate(sentences[n % 3], sid=0, speed=1.0)
        el = time.monotonic() - t0
        rtf.append(el / max(len(a.samples) / tts.sample_rate, 0.1))
        n += 1
        time.sleep(0.3)
    results["tts"] = {"count": n, "rtf_median": statistics.median(rtf) if rtf else 0}


# ---- 负载 2：ASR 解码循环（CPU）----
def asr_load(model, pcm, results):
    rec = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=f"{model}/tokens.txt",
        encoder=f"{model}/encoder-epoch-99-avg-1.onnx",
        decoder=f"{model}/decoder-epoch-99-avg-1.onnx",
        joiner=f"{model}/joiner-epoch-99-avg-1.onnx",
        num_threads=2, sample_rate=16000, feature_dim=80,
        decoding_method="greedy_search")
    n, rtf = 0, []
    while not STOP.is_set():
        t0 = time.monotonic()
        stream = rec.create_stream()
        stream.accept_waveform(IN_RATE, list(pcm))
        stream.input_finished()
        while rec.is_ready(stream):
            rec.decode_stream(stream)
        rtf.append((time.monotonic() - t0) / (len(pcm) / IN_RATE))
        n += 1
    results["asr"] = {"count": n, "rtf_median": statistics.median(rtf) if rtf else 0}


# ---- 负载 3：LLM 翻译循环（GPU）----
def llm_load(url, results):
    lat = []
    body = json.dumps({
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": "翻译成英文，只输出译文。/no_think"},
                     {"role": "user", "content": "我觉得这是一种错误的观点好吧"}],
        "max_tokens": 100, "temperature": 0}).encode()
    while not STOP.is_set():
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(url + "/v1/chat/completions", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                json.loads(r.read())
            lat.append(time.monotonic() - t0)
        except Exception:
            pass
        time.sleep(1.0)
    results["llm"] = {"count": len(lat),
                      "lat_median": statistics.median(lat) if lat else 0,
                      "lat_max": max(lat) if lat else 0}


# ---- 主测：RVC 实时流（UDP 喂入 + 回包统计）----
def rvc_main(backend, pcm, duration, results, api_base):
    # 后端只把回包发往"首个客户端"；重开流让本进程成为 first client
    for mode in ("idle", "rt_rvc"):
        req = urllib.request.Request(
            api_base + "/api/mode/switch", method="POST",
            data=json.dumps({"mode": mode, "model_id": "de_narrator"}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        time.sleep(1.0)
    host, port = backend.rsplit(":", 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.02)
    sock.connect((host, int(port)))

    out_real = []          # 真转换块（非全零）
    underruns = 0
    def recv_loop():
        nonlocal underruns
        while not STOP.is_set():
            try:
                pkt, _ = sock.recvfrom(65536)
            except socket.timeout:
                continue
            if len(pkt) != HEADER.size + OUT_SAMPLES * 4:
                continue
            pts = HEADER.unpack(pkt[:HEADER.size])[1]
            samples = np.frombuffer(pkt[HEADER.size:], dtype=np.float32)
            now = time.monotonic()
            if np.abs(samples).max() > 1e-4:
                out_real.append((now, pts))
            else:
                underruns += 1

    threading.Thread(target=recv_loop, daemon=True).start()

    n_total = len(pcm) // IN_SAMPLES
    idx = 0
    loops = int(duration * IN_RATE) // (n_total * IN_SAMPLES) + 1
    t_start = time.monotonic()
    send_elapsed = 0.0
    for _ in range(loops):
        if STOP.is_set() or time.monotonic() - t_start > duration:
            break
        for k in range(n_total):
            if time.monotonic() - t_start > duration:
                break
            hdr = HEADER.pack(MZRT_MAGIC, time.monotonic_ns(), idx + 1, 1, 200, 255, 1)
            sock.send(hdr + pcm[k * IN_SAMPLES:(k + 1) * IN_SAMPLES].tobytes())
            idx += 1
            # 20ms 实时节拍（扣除已耗时间，避免计时漂移）
            target = t_start + idx * 0.02
            send_elapsed = time.monotonic() - t_start
            if target > time.monotonic():
                time.sleep(target - time.monotonic())
    STOP.set()
    feed_duration = idx * 0.02
    time.sleep(1.0)  # 收尾

    # 输出 RTF：真转换块（非欠载补零）覆盖的音频时长 / 喂入时长
    out_rtf = len(out_real) * 0.02 / max(feed_duration, 0.1)
    # 真块与欠载块是交错的；真块到达间隔 = 推理节奏
    gaps = [out_real[i][0] - out_real[i - 1][0] for i in range(1, len(out_real))]
    results["rvc"] = {
        "feed_s": feed_duration,
        "real_blocks": len(out_real),
        "real_s": len(out_real) * 0.02,
        "underrun_frames": underruns,
        "out_rtf": out_rtf,
        "first_real_s": (out_real[0][0] - t_start) if out_real else 0,
        "block_interval_median": statistics.median(gaps) if gaps else 0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", default="/tmp/opencode/pretest_stereo.wav")
    ap.add_argument("--backend", default="127.0.0.1:18000")
    ap.add_argument("--llama-url", default="http://127.0.0.1:18200")
    ap.add_argument("--api", default="http://127.0.0.1:18080")
    ap.add_argument("--tts-model", default=os.path.expanduser(
        "~/models/sherpa-onnx/vits-melo-zh_en"))
    ap.add_argument("--asr-model", default=os.path.expanduser(
        "~/models/sherpa-onnx/zipformer-zh-14M"))
    ap.add_argument("--duration", type=int, default=45)
    args = ap.parse_args()

    pcm = load_wav_16k(args.wav)
    results = {}

    threads = [
        threading.Thread(target=rvc_main,
                         args=(args.backend, pcm, args.duration, results, args.api), daemon=True),
        threading.Thread(target=tts_load,
                         args=(args.tts_model, 5.0, results), daemon=True),
        threading.Thread(target=asr_load,
                         args=(args.asr_model, pcm[:IN_RATE * 10], results), daemon=True),
        threading.Thread(target=llm_load, args=(args.llama_url, results), daemon=True),
    ]
    for t in threads:
        t.start()
    print(f"[bench] 压测 {args.duration}s：RVC 实时流 + TTS + ASR + LLM 同时跑 …")

    prev = cpu_total()
    samples = []
    while any(t.is_alive() for t in threads):
        time.sleep(5)
        pct, prev = cpu_percent_sample(prev)
        samples.append(pct)
        with open("/proc/meminfo") as f:
            mem = {l.split(":")[0]: int(l.split()[1]) for l in f.readlines()[:3]}
        ram_gb = (mem["MemTotal"] - mem["MemAvailable"]) / 1024 ** 2
        print(f"  t={len(samples)*5:3d}s cpu={pct:5.1f}% ram={ram_gb:.1f}GB", flush=True)

    print("\n========== 结果 ==========")
    r = results.get("rvc", {})
    print(f"RVC  实时流 : 喂入 {r.get('feed_s',0):.0f}s → 真转换 {r.get('real_blocks',0)} 块 "
          f"({r.get('real_s',0):.1f}s 音频)  欠载补零 {r.get('underrun_frames',0)} 帧")
    print(f"       节奏   : 首块 {r.get('first_real_s',0):.1f}s | 块间隔中位 "
          f"{r.get('block_interval_median',0):.1f}s | 有效RTF={r.get('out_rtf',0):.2f} "
          f"(<=1 = 实时)")
    t = results.get("tts", {})
    print(f"TTS  (CPU)  : {t.get('count',0)} 句  RTF中位={t.get('rtf_median',0):.2f}")
    a = results.get("asr", {})
    print(f"ASR  (CPU)  : {a.get('count',0)} 轮  RTF中位={a.get('rtf_median',0):.2f}")
    l = results.get("llm", {})
    print(f"LLM  (GPU)  : {l.get('count',0)} 次  延迟中位={l.get('lat_median',0)*1000:.0f}ms "
          f"峰值={l.get('lat_max',0)*1000:.0f}ms")
    print(f"CPU 总占用  : 均值 {statistics.mean(samples):.1f}%" if samples else "")


if __name__ == "__main__":
    main()
