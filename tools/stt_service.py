#!/usr/bin/env python3
# stt_service.py — 实时流式 ASR 订阅服务（sherpa-onnx 流式 zipformer）
# ============================================================================
# 输入：MZRT 1300B 契约包（UDP，来自 mozart-pre 的 16k 契约帧流）
# 断句：recognizer endpoint (0.6s silence / 12s maximum utterance)。
# segment_id is descriptive metadata; short VAD toggles must not split words.
# 输出：stdout 实时 partial/final；--transcript 可追加写入文件。
#
# 识别流生命周期：首包开流；endpoint 使用 recognizer.reset，保留前端状态。
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
from pathlib import Path
import wave
import array
import hashlib

import sherpa_onnx
from asr_checks import refine_with_numeric_check, polarity_audit

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
        enable_endpoint_detection=True,
        rule1_min_trailing_silence=1.2,
        rule2_min_trailing_silence=0.6,
        rule3_min_utterance_length=12.0,
    )


def main():
    ap = argparse.ArgumentParser(description="Mozart streaming STT service")
    ap.add_argument("--model", required=True, help="sherpa-onnx 模型目录")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=18100)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--final-model", default=None, help="Optional SenseVoice int8 directory to refine final utterances")
    ap.add_argument("--transcript", default=None, help="final 句追加写入的文件")
    ap.add_argument("--json", action="store_true", help="final 句以 JSON 行输出（供翻译订阅）")
    ap.add_argument('--utterance-dir', type=Path, help='Optional processed PCM16 utterance archive for diagnosis')
    args = ap.parse_args()

    # SIGTERM 走 finally：停服务时把最后一段增量切成 final 落盘（否则直接丢）
    def _sigterm(_sig, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _sigterm)

    recognizer = build_recognizer(args)
    final_recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=f"{args.final_model}/model.int8.onnx", tokens=f"{args.final_model}/tokens.txt",
        num_threads=args.threads, language="zh", use_itn=True,
    ) if args.final_model else None
    utterance_pcm = []
    utterance_start_pts = None
    utterance_end_pts = None
    if args.utterance_dir:
        args.utterance_dir.mkdir(parents=True,exist_ok=True)
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
    last_partial_emit = 0.0
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
        """Emit the decoded utterance at recognizer endpoint or transport end."""
        nonlocal finals, cut_offset, last_partial, current_seg, utterance_pcm, utterance_start_pts
        if stream is None:
            return
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        text = recognizer.get_result(stream)
        if len(text) < cut_offset:   # 贪心解码对已切前缀的罕见回退，防负切片
            cut_offset = len(text)
        delta = text[cut_offset:].strip()
        online_text = delta
        final_seconds = 0.0
        numeric_check = None
        if final_recognizer and utterance_pcm:
            began = time.monotonic()
            delta, numeric_check = refine_with_numeric_check(final_recognizer,
                [sample for frame in utterance_pcm for sample in frame], online_text)
            final_seconds = time.monotonic()-began
        captured_pcm = utterance_pcm
        captured_start = utterance_start_pts
        utterance_pcm = []
        utterance_start_pts = None
        cut_offset = len(text)
        last_partial = ""
        if delta:
            finals += 1
            diagnostic = {'emitted_at':time.time(), 'audio_start_pts_ns':captured_start,
                          'audio_end_pts_ns':utterance_end_pts}
            if numeric_check is not None:
                diagnostic['asr_numeric_audit'] = numeric_check
            polarity = polarity_audit(online_text, delta) if final_recognizer else None
            if polarity is not None:
                diagnostic['asr_polarity_audit'] = polarity
            if args.utterance_dir and captured_pcm:
                path=args.utterance_dir/f'{finals:04d}.wav'
                samples=array.array('h',(max(-32768,min(32767,round(sample*32768)))
                                          for frame in captured_pcm for sample in frame))
                if sys.byteorder != 'little':samples.byteswap()
                with wave.open(str(path),'wb') as audio:
                    audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000)
                    audio.writeframes(samples.tobytes())
                diagnostic.update(source_audio=str(path),source_audio_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                  source_audio_seconds=len(samples)/16000)
            print(f"\n[FINAL#{finals}] {delta}", flush=True)
            if transcript_f:
                transcript_f.write(delta + "\n")
                transcript_f.flush()
            if args.json:
                print(json.dumps({"type": "final", "seq": finals, "text": delta, "online_text": online_text,
                    "final_engine": "sensevoice" if final_recognizer else "zipformer",
                    **diagnostic,
                    "final_decode_ms": round(final_seconds*1000)},
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
        nonlocal last_partial, last_partial_emit
        text = recognizer.get_result(stream)
        delta = text[cut_offset:]
        now = time.monotonic()
        if delta != last_partial and now-last_partial_emit >= .1:
            last_partial = delta
            last_partial_emit = now
            if args.json:
                if delta.strip():
                    print(json.dumps({'type': 'partial', 'seq': finals+1, 'text': delta,
                        'emitted_at': time.time(), 'audio_start_pts_ns': utterance_start_pts,
                        'audio_end_pts_ns': utterance_end_pts}, ensure_ascii=False), flush=True)
            else:
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
                        finish_stream()
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

            current_seg = seg

            if stream is not None:
                if utterance_start_pts is None:utterance_start_pts=pts
                utterance_end_pts=pts+20_000_000
                utterance_pcm.append(pcm)
                if len(utterance_pcm) > 700:
                    raise RuntimeError('ASR utterance buffer exceeded 14 seconds')
                stream.accept_waveform(16000, list(pcm))
                while recognizer.is_ready(stream):
                    recognizer.decode_stream(stream)
                show_partial()
                if recognizer.is_endpoint(stream):
                    cut_final()
                    recognizer.reset(stream)
                    cut_offset = 0
                    last_partial = ""
    except KeyboardInterrupt:
        pass
    finally:
        finish_stream()
        if transcript_f:
            transcript_f.close()
        print(f"\n[stt] exit: packets={packets} finals={finals}", flush=True)


if __name__ == "__main__":
    main()
