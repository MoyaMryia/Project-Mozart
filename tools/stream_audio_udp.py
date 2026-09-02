#!/usr/bin/env python3
"""Stream a WAV through Mozart's realtime UDP contract and write the replies.

The control plane is HTTP, but audio does not travel over HTTP.  This tool
activates RT_RVC through the control API, sends 16 kHz mono float32 MZRT
packets at a 20 ms cadence, receives 48 kHz mono float32 MZRT packets, and
writes the received stream as a WAV file.
"""

import argparse
import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf


MAGIC = 0x4D5A5254
HEADER = struct.Struct("<IQIBBBB")
IN_RATE = 16_000
OUT_RATE = 48_000
IN_SAMPLES = 320
OUT_SAMPLES = 960
PACKET_SIZE = HEADER.size + IN_SAMPLES * 4
OUTPUT_PACKET_SIZE = HEADER.size + OUT_SAMPLES * 4


def request_json(url: str, method: str = "GET", body: object | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {} if data is None else {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} returned HTTP {error.code}: {detail}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"{method} {url} returned a non-object JSON response")
    return payload


def load_input(path: str) -> np.ndarray:
    audio, source_rate = sf.read(path, dtype="float32", always_2d=True)
    audio = np.ascontiguousarray(audio.mean(axis=1), dtype=np.float32)
    if source_rate != IN_RATE:
        audio = librosa.resample(audio, orig_sr=source_rate, target_sr=IN_RATE)
    audio = np.asarray(audio, dtype=np.float32)
    peak = float(np.max(np.abs(audio), initial=0.0))
    if peak > 1.0:
        audio = audio / peak
    return np.ascontiguousarray(audio)


def packet(frame_idx: int, samples: np.ndarray, pts_ns: int, voiced: bool) -> bytes:
    payload = np.zeros(IN_SAMPLES, dtype=np.float32)
    count = min(IN_SAMPLES, samples.size)
    payload[:count] = samples[:count]
    header = HEADER.pack(
        MAGIC,
        pts_ns,
        frame_idx,
        1 if voiced else 0,
        220 if voiced else 0,
        255 if voiced else 0,
        1,
    )
    return header + payload.tobytes()


def parse_output(data: bytes) -> tuple[int, np.ndarray] | None:
    if len(data) != OUTPUT_PACKET_SIZE:
        return None
    magic, _pts_ns, frame_idx, _vad, _energy, _conf, _segment = HEADER.unpack_from(data)
    if magic != MAGIC:
        return None
    samples = np.frombuffer(data, dtype="<f4", offset=HEADER.size, count=OUT_SAMPLES)
    return frame_idx, np.array(samples, dtype=np.float32, copy=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="input WAV or another libsndfile-readable audio file")
    parser.add_argument("output", help="output WAV, written at 48 kHz float32")
    parser.add_argument("--backend", default="127.0.0.1:18000", help="UDP host:port")
    parser.add_argument("--api", default="http://127.0.0.1:18080", help="backend HTTP base URL")
    parser.add_argument("--model-id", default="", help="model directory ID sent to RT_RVC")
    parser.add_argument("--flush-seconds", type=float, default=2.0,
                        help="silence sent after input to drain the 2 s backend window")
    parser.add_argument("--timeout", type=float, default=15.0,
                        help="maximum seconds to wait for missing UDP replies")
    parser.add_argument("--no-activate", action="store_true",
                        help="do not call /api/mode/switch; require RT_RVC to be active")
    parser.add_argument("--no-pace", action="store_true",
                        help="send packets as fast as possible instead of every 20 ms")
    parser.add_argument("--no-trim-leading", action="store_true",
                        help="keep the initial cold-start silence in the WAV")
    parser.add_argument("--allow-missing", action="store_true",
                        help="write zero-filled gaps instead of failing on UDP loss")
    args = parser.parse_args()

    host, port_text = args.backend.rsplit(":", 1)
    port = int(port_text)
    audio = load_input(args.input)
    input_frames = (audio.size + IN_SAMPLES - 1) // IN_SAMPLES
    flush_frames = max(0, round(args.flush_seconds * IN_RATE / IN_SAMPLES))
    total_frames = input_frames + flush_frames

    if not args.no_activate:
        result = request_json(
            args.api.rstrip("/") + "/api/mode/switch",
            method="POST",
            body={"mode": "rt_rvc", "model_id": args.model_id},
        )
        if result.get("status") not in ("active",):
            raise RuntimeError(f"backend did not activate RT_RVC: {result}")

    received: dict[int, np.ndarray] = {}
    receive_errors: list[BaseException] = []
    done = threading.Event()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.2)

    def receive_loop() -> None:
        while not done.is_set():
            try:
                data, _address = sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError as error:
                if not done.is_set():
                    receive_errors.append(error)
                return
            parsed = parse_output(data)
            if parsed is not None:
                received[parsed[0]] = parsed[1]

    receiver = threading.Thread(target=receive_loop, name="mozart-udp-receiver")
    receiver.start()
    started = time.monotonic()
    pts_ns = time.monotonic_ns()
    try:
        for frame_idx in range(total_frames):
            start = frame_idx * IN_SAMPLES
            end = min(start + IN_SAMPLES, audio.size)
            voiced = frame_idx < input_frames
            sock.sendto(packet(frame_idx, audio[start:end], pts_ns, voiced), (host, port))
            pts_ns += 20_000_000
            if not args.no_pace:
                target = started + (frame_idx + 1) * 0.02
                time.sleep(max(0.0, target - time.monotonic()))

        deadline = time.monotonic() + args.timeout
        while len(received) < total_frames and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        done.set()
        sock.close()
        receiver.join(timeout=1.0)

    if receive_errors:
        raise RuntimeError(f"UDP receive failed: {receive_errors[0]}")

    missing = [idx for idx in range(total_frames) if idx not in received]
    if missing and not args.allow_missing:
        preview = missing[:10]
        suffix = "..." if len(missing) > len(preview) else ""
        raise RuntimeError(
            f"received {len(received)}/{total_frames} UDP frames; "
            f"missing frame indices {preview}{suffix}. "
            "Use --allow-missing to write zero-filled gaps."
        )
    output = np.zeros(total_frames * OUT_SAMPLES, dtype=np.float32)
    for frame_idx, samples in received.items():
        if 0 <= frame_idx < total_frames:
            start = frame_idx * OUT_SAMPLES
            output[start:start + OUT_SAMPLES] = samples

    if not args.no_trim_leading:
        # RT_RVC needs one 2 s window before its first non-zero block.  Keep
        # the stream packet indices intact above, then make the file useful to
        # audition by removing that deterministic startup latency.
        trim = min(output.size, 2 * OUT_RATE)
        output = output[trim:]

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output_path, output, OUT_RATE, subtype="FLOAT")
    print(json.dumps({
        "input": str(Path(args.input).resolve()),
        "output": str(output_path.resolve()),
        "input_frames": input_frames,
        "flush_frames": flush_frames,
        "received_frames": len(received),
        "missing_frames": missing,
        "output_samples": int(output.size),
    }, indent=2))


if __name__ == "__main__":
    main()
