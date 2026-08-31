#!/usr/bin/env python3
# demo_fullchain.py — 全链吹 B 演示：中文语音 → ASR → LLM 翻译 → TTS → RVC 变声 → 播放
# ============================================================================
# 一条命令串起板上的四条算力路径：
#   [ASR]  sherpa-onnx 流式 zipformer (CPU)
#   [LLM]  llama-server Qwen3.5-0.8B (GPU, 常驻 :18200)
#   [TTS]  sherpa-onnx Matcha zh-baker (CPU, RTF ~0.2)
#   [RVC]  rvc-backend 滑窗流式 ONNX (CPU/GPU, rt_rvc :18000)  ← 回包播放
#
# 输入：--input xxx.wav（48k/立体声/16bit，或 16k 单声道 f32）
#      或 --mic hw:2,0 --seconds N（N 秒后自动截断处理）
# 前置：rvc-backend 已运行且 rt_rvc active；llama-server 已监听 :18200
import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request

import numpy as np
import sherpa_onnx
import soundfile as sf

MZRT_MAGIC = 0x4D5A5254
HEADER = struct.Struct("<IQIBBBB")
IN_SAMPLES, IN_RATE, OUT_SAMPLES, OUT_RATE = 320, 16000, 960, 48000
SENT_PROMPT = ("你是同传翻译引擎。把用户的中文逐句翻译成英文，只输出英文译文，"
               "不要解释、不要引号。/no_think")


def log(stage, msg):
    print(f"[{stage}] {msg}", flush=True)


# ---- 输入：WAV 或 arecord ----
def load_input(args) -> np.ndarray:
    """返回 16k 单声道 float32。"""
    if args.input:
        data, sr = sf.read(args.input, dtype="float32", always_2d=True)
        mono = data[:, 0] if data.shape[1] > 1 else data[:, 0]
        if sr != IN_RATE:
            mono = resample_linear(mono, sr, IN_RATE)
        return mono
    # 麦克风：arecord 48k/立体声/16bit，录 seconds 秒
    log("MIC", f"录制 {args.seconds}s @ {args.mic} …说完就等它截断")
    raw = subprocess.run(
        ["arecord", "-D", args.mic, "-f", "S16_LE", "-r", "48000", "-c", "2",
         "-d", str(args.seconds), "-t", "raw"],
        capture_output=True).stdout
    stereo = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2)
    mono = stereo[:, 0].astype(np.float32) / 32768.0
    return resample_linear(mono, 48000, IN_RATE)


def resample_linear(x, src, dst):
    if src == dst:
        return x.astype(np.float32)
    n_out = int(len(x) * dst / src)
    pos = np.linspace(0, len(x) - 1, n_out)
    i0 = pos.astype(np.int64)
    i1 = np.minimum(i0 + 1, len(x) - 1)
    frac = pos - i0
    return (x[i0] * (1 - frac) + x[i1] * frac).astype(np.float32)


# ---- [1] 分句：能量滞回（门限 = 噪声底 + 8dB，自适应）----
def segment(pcm, hangover_s=0.5, min_s=0.4):
    frames = len(pcm) // IN_SAMPLES
    rms = np.array([
        np.sqrt(np.mean(pcm[i * IN_SAMPLES:(i + 1) * IN_SAMPLES] ** 2) + 1e-12)
        for i in range(frames)])
    db = 20 * np.log10(rms + 1e-12)
    p10, p90 = np.percentile(db, [10, 90])
    gate = max(-45.0, min(p10 + 0.5 * (p90 - p10), -15.0))
    voiced = db > gate
    hang = int(hangover_s * 1000 / 20)   # 帧数
    minf = int(min_s * 1000 / 20)
    segments, start, quiet = [], None, 0
    for i, v in enumerate(voiced):
        if v:
            if start is None:
                start = i
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet >= hang:
                if i - quiet + 1 - start >= minf:
                    segments.append((start, i - quiet + 1))
                start, quiet = None, 0
    if start is not None and frames - start >= minf:
        segments.append((start, frames))
    if not segments and frames >= minf:
        segments = [(0, frames)]     # 无法分句（噪声底≈语音）：整段当一句
    return [(s * IN_SAMPLES, e * IN_SAMPLES) for s, e in segments]


# ---- [2] ASR ----
def build_asr(model, threads=2):
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=f"{model}/tokens.txt",
        encoder=f"{model}/encoder-epoch-99-avg-1.onnx",
        decoder=f"{model}/decoder-epoch-99-avg-1.onnx",
        joiner=f"{model}/joiner-epoch-99-avg-1.onnx",
        num_threads=threads, sample_rate=16000, feature_dim=80,
        decoding_method="greedy_search")


def asr_sentence(recognizer, pcm):
    stream = recognizer.create_stream()
    stream.accept_waveform(IN_RATE, list(pcm))
    stream.input_finished()
    while recognizer.is_ready(stream):
        recognizer.decode_stream(stream)
    return recognizer.get_result(stream).strip()


# ---- [3] LLM 翻译 ----
def translate(url, text):
    body = json.dumps({
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": SENT_PROMPT},
                     {"role": "user", "content": text}],
        "max_tokens": 200, "temperature": 0,
    }).encode()
    req = urllib.request.Request(url + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"].strip(), time.monotonic() - t0


# ---- [4] TTS（melo：中英混读；Matcha 是纯中文词库读不了英文）----
def build_tts(model, threads=4):
    return sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=f"{model}/model.int8.onnx",
                lexicon=f"{model}/lexicon.txt",
                tokens=f"{model}/tokens.txt",
                data_dir=f"{model}/dict",
                dict_dir=f"{model}/dict",
                noise_scale=0.667,
                noise_scale_w=0.8,
                length_scale=1.0),
            num_threads=threads)))


# ---- [5] RVC：两种模式 ----
# file 模式（默认）：TTS wav → 后端 /api/file/convert（CPU RTF≈5，慢但必出声）
# udp  模式：实时契约帧喂入（需 GPU 化的 P1 才追得上实时，否则大量欠载）
class RvcFile:
    def __init__(self, api_base, workdir="/tmp/opencode/rvcfull"):
        self.api = api_base
        self.workdir = workdir
        os.makedirs(workdir, exist_ok=True)
        self.counter = 0

    def convert_and_play(self, wav_path):
        self.counter += 1
        # 峰值归一化到 0.9：melo 输出安静 + rms_mix_rate 会把安静包络带进
        # 变声输出，不拉起来最后只剩 ~0.03 峰值的"气音"
        import soundfile as sf
        norm_path = wav_path.replace(".wav", "_norm.wav")
        data, sr = sf.read(wav_path, dtype="float32")
        peak = float(np.abs(data).max()) or 1.0
        sf.write(norm_path, data * (0.9 / peak), sr)
        with open(norm_path, "rb") as f:
            # multipart 手工构造（避免引入 requests）
            boundary = "----mozartdemo"
            body = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="audio_file"; filename="utt.wav"\r\n'
                f"Content-Type: audio/wav\r\n\r\n"
            ).encode() + f.read() + (
                f"\r\n--{boundary}\r\n"
                f'Content-Disposition: form-data; name="model_id"\r\n\r\n'
                f"de_narrator\r\n"
                f"--{boundary}--\r\n"
            ).encode()
        req = urllib.request.Request(
            self.api + "/api/file/convert", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            job = json.loads(r.read())["job_id"]
        while True:
            time.sleep(2)
            with urllib.request.urlopen(
                    f"{self.api}/api/file/status?job_id={job}", timeout=10) as r:
                st = json.loads(r.read())
            if st.get("status") in ("completed", "failed"):
                if st.get("status") == "failed":
                    raise RuntimeError(st.get("error", "convert failed"))
                break
        out = os.path.join(self.workdir, f"converted_{self.counter}.wav")
        urllib.request.urlretrieve(f"{self.api}{st['download_url']}", out)
        subprocess.run(["aplay", "-q", "-D", "plughw:1,3", out])
        return out


class RvcLink:
    def __init__(self, backend):
        b_host, b_port = backend.rsplit(":", 1)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.05)
        self.sock.connect((b_host, int(b_port)))
        self.aplay = subprocess.Popen(
            ["aplay", "-q", "-t", "raw", "-f", "FLOAT_LE",
             "-r", str(OUT_RATE), "-c", "1", "-D", "plughw:1,3"],
            stdin=subprocess.PIPE)
        self.frame_idx = 0
        self.pts_ns = time.monotonic_ns()
        self.out_blocks = 0
        self.stop = False
        threading.Thread(target=self._recv, daemon=True).start()

    def _recv(self):
        while not self.stop:
            try:
                pkt, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            if len(pkt) != HEADER.size + OUT_SAMPLES * 4:
                continue
            try:
                self.aplay.stdin.write(pkt[HEADER.size:])
                self.aplay.stdin.flush()
                self.out_blocks += 1
            except BrokenPipeError:
                return

    def send_voice(self, pcm16k):
        peak = float(np.max(np.abs(pcm16k))) if len(pcm16k) else 0.0
        if peak > 1.0:
            pcm16k = pcm16k / peak
        n = len(pcm16k) // IN_SAMPLES
        silence = np.zeros(IN_SAMPLES, dtype=np.float32)
        for k in range(n + 50):     # +1s 静音尾巴顶出滑窗
            chunk = pcm16k[k * IN_SAMPLES:(k + 1) * IN_SAMPLES] if k < n else silence
            vad = 1 if k < n else 0
            hdr = HEADER.pack(MZRT_MAGIC, self.pts_ns, self.frame_idx + 1,
                              vad, 220 if vad else 0, 255 if vad else 0,
                              1 if vad else 0)
            self.sock.send(hdr + chunk.tobytes())
            self.frame_idx += 1
            self.pts_ns += 20_000_000
            time.sleep(0.02)
        return n * 0.02


def main():
    ap = argparse.ArgumentParser(description="全链演示：语音→ASR→翻译→TTS→变声")
    ap.add_argument("--input", default=None, help="输入 WAV（默认用麦克风）")
    ap.add_argument("--mic", default="hw:2,0")
    ap.add_argument("--seconds", type=int, default=8, help="麦克风模式录制秒数")
    ap.add_argument("--asr-model", default=os.path.expanduser(
        "~/models/sherpa-onnx/zipformer-zh-14M"))
    ap.add_argument("--tts-model", default=os.path.expanduser(
        "~/models/sherpa-onnx/vits-melo-zh_en"))
    ap.add_argument("--llama-url", default="http://127.0.0.1:18200")
    ap.add_argument("--backend", default="127.0.0.1:18000")
    ap.add_argument("--api", default="http://127.0.0.1:18080")
    ap.add_argument("--rvc", choices=["file", "udp"], default="file",
                    help="file=离线转换稳出声（CPU 可用）；udp=实时流（需 GPU 化 P1）")
    args = ap.parse_args()

    pcm = load_input(args)
    log("IN", f"{len(pcm)/IN_RATE:.1f}s @16k")

    t0 = time.monotonic()
    recognizer = build_asr(args.asr_model)
    log("ASR", f"模型就绪 {time.monotonic()-t0:.1f}s")

    tts = build_tts(args.tts_model)
    log("TTS", "模型就绪")

    rvc_file = RvcFile(args.api) if args.rvc == "file" else None
    rvc_udp = RvcLink(args.backend) if args.rvc == "udp" else None
    log("RVC", f"模式={args.rvc}（file→/{'api/file/convert'}，udp→{args.backend}）")

    for (s, e) in segment(pcm):
        piece = pcm[s:e]
        # ASR
        t0 = time.monotonic()
        zh = asr_sentence(recognizer, piece)
        if not zh:
            continue
        log("ASR", f"{time.monotonic()-t0:.2f}s  「{zh}」")
        # LLM 翻译
        en, el = translate(args.llama_url, zh)
        log("LLM", f"{el:.2f}s  {en}")
        # TTS
        t0 = time.monotonic()
        audio = tts.generate(en, sid=0, speed=1.0)
        tts_pcm = np.asarray(audio.samples, dtype=np.float32)
        wav_tmp = f"/tmp/opencode/utt_{int(time.time()*1000)}.wav"
        sf.write(wav_tmp, tts_pcm, tts.sample_rate)
        log("TTS", f"{time.monotonic()-t0:.2f}s  {len(tts_pcm)/tts.sample_rate:.1f}s 音频")
        # RVC 变声
        t0 = time.monotonic()
        if rvc_file:
            out = rvc_file.convert_and_play(wav_tmp)
            log("RVC", f"{time.monotonic()-t0:.1f}s 转换完成，已播放 {out}")
        else:
            dur = rvc_udp.send_voice(pcm16k := resample_linear(
                tts_pcm, tts.sample_rate, IN_RATE))
            log("RVC", f"喂入 {dur:.1f}s（实时节拍），已收回包 "
                      f"{rvc_udp.out_blocks*0.02:.1f}s，播放中…")
            time.sleep(3)

    print("[demo] 完成。", flush=True)


if __name__ == "__main__":
    main()
