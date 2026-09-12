#!/usr/bin/env python3
# stt_service.py — 实时流式 ASR 订阅服务（sherpa-onnx 流式 zipformer）
# ============================================================================
# 输入：MZRT 1300B 契约包（UDP，来自 mozart-pre 的 16k 契约帧流）
# 断句：meta.segment_id 驱动（mozart-pre 滞回分段：说话=N，静音=0）。
#       段切换 / 1s 空闲 = 切出 final 句（累计文本的增量）。
# 输出：stdout 实时 partial/final；--transcript 可追加写入文件。
#
# 识别流生命周期：连接级 —— 首包即 create_stream，段切换不重置流。
# 约束：zipformer-zh-14M 流式识别器必须从连接的第一个包开始喂数据，
#       从语音中段开流（旧版按 seg!=0 才开流）会整段输出为空
#       （2026-09-12 实测：跳过开头 40ms 即全空）。
#
# 用法：
#   .venv/bin/python tools/stt_service.py \
#       --model ~/models/sherpa-onnx/zipformer-zh-14M \
#       --port 18100 [--transcript subs.txt]
#
# 供后续 Qwen 翻译订阅的 final 句同时打印为 JSON 行（--json 开启）。
import argparse
import json
import signal
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

    # SIGTERM 走 finally：停服务时把最后一段增量切成 final 落盘（否则直接丢）
    def _sigterm(_sig, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _sigterm)

    recognizer = build_recognizer(args)
    tail_paddings = [0.0] * int(16000 * 0.3)  # 收尾 0.3s，吐出帧边界残留

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
    sock.bind((args.host, args.port))
    sock.settimeout(0.1)
    print(f"[stt] listening on {args.host}:{args.port}, model={args.model}", flush=True)

    stream = None
    current_seg = None   # 最近看到的段 id（0=静音），仅用于切句判定
    seg_start_pts = 0
    packets = 0
    finals = 0
    last_report = time.monotonic()
    last_packet = 0.0
    last_partial = ""
    cut_offset = 0       # 已切出 final 的累计字符位置（get_result 是全连接累计文本）
    transcript_f = open(args.transcript, "a", encoding="utf-8") if args.transcript else None

    def start_stream(pts):
        nonlocal stream, current_seg, seg_start_pts, cut_offset, last_partial
        stream = recognizer.create_stream()
        current_seg = 0
        seg_start_pts = pts
        cut_offset = 0
        last_partial = ""

    def cut_final():
        """段切换 / 空闲兜底：把累计文本相对上次切点的新增部分作为一句切出。
        不重置识别流（模型约束见文件头），也不用 tail+input_finished——
        后续音频还在流入，跨切点的尾词会落入下一句。"""
        nonlocal finals, cut_offset, last_partial, current_seg
        if stream is None:
            return
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        text = recognizer.get_result(stream)
        if len(text) < cut_offset:   # 贪心解码对已切前缀的罕见回退，防负切片
            cut_offset = len(text)
        delta = text[cut_offset:].strip()
        cut_offset = len(text)
        last_partial = ""
        if delta:
            finals += 1
            print(f"\n[FINAL#{finals}] {delta}", flush=True)
            if transcript_f:
                transcript_f.write(delta + "\n")
                transcript_f.flush()
            if args.json:
                print(json.dumps({"type": "final", "seq": finals, "text": delta},
                                 ensure_ascii=False), flush=True)

    def finish_stream():
        """连接结束：喂 tail + input_finished，切出最后一段增量。"""
        nonlocal stream, current_seg
        if stream is None:
            return
        stream.accept_waveform(16000, tail_paddings)
        stream.input_finished()
        cut_final()
        stream = None
        current_seg = None

    def show_partial():
        nonlocal last_partial
        text = recognizer.get_result(stream)
        delta = text[cut_offset:]
        if delta != last_partial:
            last_partial = delta
            print("\r[PART] " + delta, end="", flush=True)

    try:
        while True:
            try:
                pkt, _ = sock.recvfrom(65536)
                last_packet = time.monotonic()
            except socket.timeout:
                # 空闲：推进解码；说话中断 >1s 兜底切句（段元数据可能迟到）
                if stream is not None:
                    if recognizer.is_ready(stream):
                        recognizer.decode_stream(stream)
                    if time.monotonic() - last_packet > 1.0:
                        cut_final()
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

            if stream is None:
                start_stream(pts)        # 连接首包即开流（模型约束，见文件头）
            elif seg != current_seg:
                cut_final()              # 段切换 → 切出一句（流不重置）
            current_seg = seg

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
