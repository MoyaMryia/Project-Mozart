#!/usr/bin/env python3
# tts_service.py — 最小 TTS 服务（sherpa-onnx VITS melo，中英）
# ============================================================================
# 独立进程：stdin 收句（一行一句），合成 WAV 到输出目录并打印耗时。
# demo 用途：字幕路的"读出来"开关；将来接播放队列/RVC 链。
#
# 用法：
#   .venv/bin/python tools/tts_service.py \
#       --model ~/models/sherpa-onnx/vits-melo-zh_en --out-dir /tmp/opencode/tts
#   然后往 stdin 敲句子，回车合成；Ctrl-C 退出。
import argparse
import os
import sys
import time

import sherpa_onnx
import soundfile as sf


def main():
    ap = argparse.ArgumentParser(description="Mozart minimal TTS service")
    ap.add_argument("--model", required=True)
    ap.add_argument("--out-dir", default="/tmp/opencode/tts")
    ap.add_argument("--sid", type=int, default=0, help="说话人 id")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--play", action="store_true", help="合成后尝试 aplay 播放")
    # kokoro 等模型需要：voices.bin / 多词典 / espeak-ng-data / 规则 FST
    ap.add_argument("--voices", default=None)
    ap.add_argument("--lexicon", default=None, help="逗号分隔多词典")
    ap.add_argument("--data-dir", default=None, help="espeak-ng-data 目录")
    ap.add_argument("--rule-fsts", default=None, help="逗号分隔 FST")
    ap.add_argument("--engine", choices=["vits", "kokoro", "matcha"], default="matcha")
    ap.add_argument("--vocoder", default=None, help="matcha 需要 hifigan vocoder")
    ap.add_argument("--acoustic", default=None, help="matcha acoustic model 文件名")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    m = args.model
    if args.engine == "kokoro":
        kokoro_cfg = sherpa_onnx.OfflineTtsKokoroModelConfig(
            model=f"{m}/model.int8.onnx",
            voices=args.voices or f"{m}/voices.bin",
            tokens=f"{m}/tokens.txt",
            lexicon=args.lexicon or "",
            data_dir=args.data_dir or f"{m}/espeak-ng-data",
            dict_dir=f"{m}/dict",
            length_scale=1.0 / args.speed,
        )
        model_cfg = sherpa_onnx.OfflineTtsModelConfig(kokoro=kokoro_cfg, num_threads=args.threads)
    else:
        vits_cfg = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=f"{m}/model.int8.onnx",
            lexicon=args.lexicon or f"{m}/lexicon.txt",
            tokens=f"{m}/tokens.txt",
            data_dir=args.data_dir or f"{m}/dict",
            dict_dir=f"{m}/dict",
            noise_scale=0.667,
            noise_scale_w=0.8,
            length_scale=1.0 / args.speed,
        )
        model_cfg = sherpa_onnx.OfflineTtsModelConfig(vits=vits_cfg, num_threads=args.threads)

    if args.engine == "matcha":
        matcha_cfg = sherpa_onnx.OfflineTtsMatchaModelConfig(
            acoustic_model=args.acoustic or f"{m}/model-steps-3.onnx",
            vocoder=args.vocoder or f"{m}/hifigan_v2.onnx",
            lexicon=args.lexicon or f"{m}/lexicon.txt",
            tokens=f"{m}/tokens.txt",
            data_dir=f"{m}/dict",
            dict_dir=f"{m}/dict",
            noise_scale=0.667,
            length_scale=1.0 / args.speed,
        )
        model_cfg = sherpa_onnx.OfflineTtsModelConfig(matcha=matcha_cfg, num_threads=args.threads)

    tts = sherpa_onnx.OfflineTts(
        sherpa_onnx.OfflineTtsConfig(
            model=model_cfg,
            rule_fsts=args.rule_fsts or "",
        ),
    )
    sr = tts.sample_rate
    print(f"[tts] ready: sr={sr} threads={args.threads} model={args.model}", flush=True)

    n = 0
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        n += 1
        t0 = time.monotonic()
        audio = tts.generate(text, sid=args.sid, speed=args.speed)
        elapsed = time.monotonic() - t0
        dur = len(audio.samples) / sr
        rtf = elapsed / dur if dur else 0
        path = os.path.join(args.out_dir, f"utt_{n:04d}.wav")
        sf.write(path, audio.samples, sr)
        print(f"[tts#{n}] {dur:.1f}s 音频 / {elapsed:.2f}s 合成 (RTF {rtf:.2f}) -> {path}",
              flush=True)
        if args.play:
            os.system(f"aplay -q {path} &")


if __name__ == "__main__":
    main()
