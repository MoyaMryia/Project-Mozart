#!/usr/bin/env python3
# subtitle_bridge.py — 文字路胶水：STT → Qwen 翻译 → 字幕 + 可选语音播报
# ============================================================================
# 内部拉起 tools/stt_service.py（--json），逐句消费 final：
#
#   [stt final 中文] ──→ llama-server (/v1/chat/completions, GPU 常驻)
#        │                      ↓
#        ├─→ 字幕 JSON 行（stdout + subtitles.jsonl，供前端 WebSocket）
#        └─→ (--speak) Matcha TTS 合成译文 → aplay HDMI 播放
#
# 用法：
#   .venv/bin/python tools/subtitle_bridge.py \
#       --stt-model ~/models/sherpa-onnx/zipformer-zh-14M \
#       [--speak --tts-model ~/models/sherpa-onnx/matcha-zh-baker]
# 测试音频：mozart-pre -i xxx.wav -a 127.0.0.1 -p <stt_port>
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

SYSTEM_PROMPT = (
    "你是同传翻译引擎。把用户的中文逐句翻译成英文，只输出英文译文，"
    "不要解释、不要拼音、不要引号。口语要译得自然。/no_think"
)


def translate(url, text, timeout=10.0):
    body = json.dumps({
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "max_tokens": 200,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        url + "/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    el = time.monotonic() - t0
    return d["choices"][0]["message"]["content"].strip(), el


class Speaker:
    """Matcha TTS + aplay 播放（懒加载，~0.5s/句）"""

    def __init__(self, model, threads=4):
        import sherpa_onnx
        self.tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
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
        self.sr = self.tts.sample_rate
        self.queue = []
        self.lock = threading.Condition()
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    def speak(self, text):
        with self.lock:
            self.queue.append(text)
            self.lock.notify()

    def _loop(self):
        import soundfile as sf
        i = 0
        while True:
            with self.lock:
                while not self.queue:
                    self.lock.wait()
                text = self.queue.pop(0)
            t0 = time.monotonic()
            audio = self.tts.generate(text, sid=0, speed=1.0)
            i += 1
            path = f"/tmp/opencode/bridge_tts_{i:04d}.wav"
            sf.write(path, audio.samples, self.sr)
            os.system(f"aplay -q -D plughw:1,3 {path} 2>/dev/null")
            print(f"[speak] {time.monotonic()-t0:.2f}s <- {text[:40]}",
                  file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser(description="STT→翻译→字幕/语音 胶水服务")
    ap.add_argument("--stt-model", required=True)
    ap.add_argument("--stt-port", type=int, default=18100)
    ap.add_argument("--llama-url", default="http://127.0.0.1:18200")
    ap.add_argument("--jsonl", default="/tmp/opencode/subtitles.jsonl",
                    help="字幕 JSON 行输出文件")
    ap.add_argument("--speak", action="store_true", help="译文用 TTS 播报")
    ap.add_argument("--tts-model", default=os.path.expanduser(
        "~/models/sherpa-onnx/matcha-zh-baker"))
    ap.add_argument("--lang", choices=["en", "zh"], default="en",
                    help="播报语言：en=读译文，zh=读原文")
    args = ap.parse_args()

    speaker = Speaker(args.tts_model) if args.speak else None
    jsonl = open(args.jsonl, "a", encoding="utf-8")

    cmd = [os.path.join(SCRIPT_DIR, "stt_service.py"),
           "--model", args.stt_model, "--port", str(args.stt_port), "--json"]
    env = dict(os.environ)
    stt = subprocess.Popen([sys.executable] + cmd,
                           stdout=subprocess.PIPE, stderr=sys.stderr,
                           text=True, env=env)
    print(f"[bridge] stt pid={stt.pid} port={args.stt_port} llama={args.llama_url} "
          f"speak={args.speak} jsonl={args.jsonl}", file=sys.stderr, flush=True)

    seq = 0
    for line in stt.stdout:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "final":
            continue
        zh = ev["text"]
        try:
            en, el = translate(args.llama_url, zh)
        except Exception as e:  # 翻译挂了不挡字幕
            en, el = "", 0.0
            print(f"[bridge] translate failed: {e}", file=sys.stderr, flush=True)
        seq += 1
        record = {"seq": seq, "zh": zh, "en": en, "translate_ms": int(el * 1000),
                  "ts": time.strftime("%H:%M:%S")}
        print(json.dumps(record, ensure_ascii=False), flush=True)
        jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
        jsonl.flush()
        if speaker:
            speaker.speak(en if (en and args.lang == "en") else zh)

    stt.wait()
    print(f"[bridge] stt exited rc={stt.returncode}", file=sys.stderr)


if __name__ == "__main__":
    main()
