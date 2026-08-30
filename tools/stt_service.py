#!/usr/bin/env python3
# stt_service.py — 实时流式 ASR 订阅服务（sherpa-onnx 流式 zipformer）
# ============================================================================
# 输入：MZRT 1300B 契约包（UDP，来自 mozart-pre 的 16k 契约帧流）
# 断句：meta.segment_id 驱动（mozart-pre 滞回分段：说话=N，静音=0）。
#       不再挂第二套 VAD；段切换 = 出 final 句。
# 输出：stdout 实时 partial/final；--transcript 可追加写入文件。
#
# 用法：
#   .venv/bin/python tools/stt_service.py \
#       --model ~/models/sherpa-onnx/zipformer-zh-14M \
#       --port 18100 [--transcript subs.txt]
#
# 供后续 Qwen 翻译订阅的 final 句同时打印为 JSON 行（--json 开启）。
import argparse
import json
import socket
import struct
import sys
import time

import sherpa_onnx

MZRT_MAGIC = 0x4D5A5254
HEADER = struct.Struct("<IQIBBBB")  # magic, pts_ns, frame_idx, vad, energy, conf, segment
FRAME_SAMPLES = 320


def build_recognizer(args):
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=f"{args.model}/tokens.txt",
        encoder=f"{args.model}/encoder-epoch-99-avg-1.onnx",
        decoder=f"{args.model}/decoder-epoch-99-avg-1.onnx",
        joiner=f"{args.model}/joiner-epoch-99-avg-1.onnx",
        num_threads=args.threads,
        sample_rate=16000,
        feature_dim=80,
        decoding_method="greedy_search",
    )


def main():
    ap = argparse.ArgumentParser(description="Mozart streaming STT service")
    ap.add_argument("--model", required=True, help="sherpa-onnx 模型目录")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=18100)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--transcript", default=None, help="final 句追加写入的文件")
    ap.add_argument("--json", action="store_true", help="final 句以 JSON 行输出（供翻译订阅）")
    args = ap.parse_args()

    recognizer = build_recognizer(args)
    tail_paddings = [0.0] * int(16000 * 0.3)  # 收尾 0.3s，吐出帧边界残留

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
    sock.bind((args.host, args.port))
    sock.settimeout(0.1)
    print(f"[stt] listening on {args.host}:{args.port}, model={args.model}", flush=True)

    stream = None
    current_seg = None
    seg_start_pts = 0
    packets = 0
    finals = 0
    last_report = time.monotonic()
    last_packet = 0.0
    last_partial = ""
    transcript_f = open(args.transcript, "a", encoding="utf-8") if args.transcript else None

    def start_stream(seg, pts):
        nonlocal stream, current_seg, seg_start_pts
        stream = recognizer.create_stream()
        current_seg = seg
        seg_start_pts = pts

    def finish_stream():
        nonlocal stream, current_seg, finals, last_partial
        if stream is None:
            return
        stream.accept_waveform(16000, tail_paddings)
        stream.input_finished()
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        text = recognizer.get_result(stream).strip()
        last_partial = ""
        if text:
            finals += 1
            print(f"\n[FINAL#{finals}] {text}", flush=True)
            if transcript_f:
                transcript_f.write(text + "\n")
                transcript_f.flush()
            if args.json:
                print(json.dumps({"type": "final", "seq": finals, "text": text},
                                 ensure_ascii=False), flush=True)
        stream = None
        current_seg = None

    def show_partial():
        nonlocal last_partial
        text = recognizer.get_result(stream)
        if text != last_partial:
            last_partial = text
            print("\r[PART] " + text, end="", flush=True)

    try:
        while True:
            try:
                pkt, _ = sock.recvfrom(65536)
                last_packet = time.monotonic()
            except socket.timeout:
                # 空闲：推进解码；说话中断 >1s 自动出 final（流结束的兜底断句）
                if stream is not None:
                    if recognizer.is_ready(stream):
                        recognizer.decode_stream(stream)
                    if time.monotonic() - last_packet > 1.0:
                        finish_stream()
                        last_partial = ""
                    elif recognizer.is_ready(stream):
                        show_partial()
                if time.monotonic() - last_report > 10:
                    print(f"\n[stt] packets={packets} finals={finals} "
                          f"pending_seg={current_seg}", flush=True)
                    last_report = time.monotonic()
                continue

            if len(pkt) != HEADER.size + FRAME_SAMPLES * 4:
                continue
            magic, pts, idx, vad, energy, conf, seg = HEADER.unpack(pkt[:HEADER.size])
            if magic != MZRT_MAGIC:
                continue
            pcm = struct.unpack(f"<{FRAME_SAMPLES}f", pkt[HEADER.size:])
            packets += 1

            if stream is None and seg != 0:
                start_stream(seg, pts)
            elif stream is not None and seg != current_seg:
                finish_stream()          # 段切换 → 出 final
                if seg != 0:
                    start_stream(seg, pts)

            if stream is not None:
                stream.accept_waveform(16000, list(pcm))
                while recognizer.is_ready(stream):
                    recognizer.decode_stream(stream)
                show_partial()
    except KeyboardInterrupt:
        pass
    finally:
        finish_stream()
        if transcript_f:
            transcript_f.close()
        print(f"\n[stt] exit: packets={packets} finals={finals}", flush=True)


if __name__ == "__main__":
    main()
