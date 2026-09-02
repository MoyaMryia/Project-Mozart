#!/usr/bin/env python3
"""Self-verification: drive the running Mozart backend and reproduce each locked
numeric standard, reporting per-standard PASS/FAIL against the manifest tolerances.

For each standard whose `repro.mode` is not "none":
  - "file"      -> POST /api/file/convert, download the result
  - "streaming" -> run tools/stream_audio_udp.py against the UDP data port
then compare the captured output to the locked WAV (envelope-aligned
correlation + autocorrelation F0 agreement + RMS). Exit 0 iff all pass.

The backend must be running (mozart_stated) with the model files in place;
this script only talks to it over HTTP/UDP. Example:

    python rvc-golden/verify_backend.py \
        --api http://127.0.0.1:18181 --udp 127.0.0.1:18101
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
SR = 16000


def _get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read()


def _post_json(url, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def switch_mode(api, mode, model_id):
    _post_json(f"{api}/api/mode/switch", {"mode": mode, "speaker_id": model_id})
    time.sleep(2.0)


def run_file(api, model_id, wav_in, dst):
    # multipart upload via curl (avoids hand-rolling boundaries)
    out = subprocess.run(
        ["curl", "-s", "-X", "POST", f"{api}/api/file/convert",
         "-F", f"audio_file=@{wav_in}", "-F", f"speaker_id={model_id}"],
        check=True, capture_output=True, text=True).stdout
    job_id = json.loads(out)["job_id"]
    for _ in range(600):
        st = json.loads(_get(f"{api}/api/file/status?job_id={job_id}"))
        if st["status"] == "completed":
            dst.write_bytes(_get(st["download_url"].replace("/api", f"{api}/api", 1)))
            return dst
        if st["status"] in ("error", "failed"):
            raise RuntimeError(f"file job failed: {st.get('error')}")
        time.sleep(2.0)
    raise RuntimeError("file job timed out")


def run_streaming(api, udp, model_id, wav_in, dst):
    subprocess.run(
        [sys.executable, str(REPO / "tools" / "stream_audio_udp.py"),
         str(wav_in), str(dst), "--backend", udp, "--api", api,
         "--model-id", model_id, "--flush-seconds", "2.0", "--no-trim-leading"],
        check=True, capture_output=True)
    return dst


def load16(path):
    a, sr = sf.read(path, dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    if sr != SR:
        from math import gcd
        g = gcd(sr, SR)
        a = resample_poly(a, SR // g, sr // g)
    return np.asarray(a, dtype=np.float32)


def _env(a, hop=1600):
    n = max(1, len(a) // hop)
    e = np.array([np.sqrt(np.mean(a[i*hop:(i+1)*hop]**2) + 1e-12) for i in range(n)])
    return np.log(e + 1e-6)


def _onset(a, thr=0.02):
    peak = np.abs(a).max() + 1e-12
    nz = np.nonzero(np.abs(a) > thr * peak)[0]
    return int(nz[0]) if len(nz) else 0


def align(ref, cand):
    r0, c0 = ref[_onset(ref):], cand[_onset(cand):]
    er, ec = _env(r0), _env(c0)
    n = min(len(er), len(ec))
    corr = np.correlate(er[:n], ec[:n], "full")
    shift = (int(np.argmax(np.abs(corr)) - (n - 1))) * 1600   # samples, cand late
    if shift >= 0:
        A, B = r0[shift:], c0[:len(r0) - shift]
    else:
        A, B = r0[:len(r0) + shift], c0[-shift:]
    m = min(len(A), len(B))
    return A[:m], B[:m]


def f0(a, frame=0.04, fmin=55.0, fmax=600.0):
    n = int(frame * SR)
    lo, hi = int(SR / fmax), int(SR / fmin)
    out = []
    for i in range(0, len(a) - n, n):
        seg = a[i:i + n]
        if np.sqrt(np.mean(seg**2)) < 1e-4:
            out.append(0.0); continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, "full")[n - 1:]
        ac /= ac[0] + 1e-12
        band = ac[lo:min(hi, len(ac) - 1)]
        out.append(SR / (int(np.argmax(band)) + lo) if band.size else 0.0)
    return np.asarray(out)


def compare(ref_path, out_path):
    a, b = load16(ref_path), load16(out_path)
    A, B = align(a, b)
    rms_a = np.sqrt(np.mean(A**2) + 1e-12)
    rms_b = np.sqrt(np.mean(B**2) + 1e-12)
    corr = float(np.mean((A - A.mean()) * (B - B.mean())) / (np.std(A) * np.std(B) + 1e-12))
    fa, fb = f0(A), f0(B)
    k = min(len(fa), len(fb)); fa, fb = fa[:k], fb[:k]
    both = (fa > 0) & (fb > 0)
    cents = float(np.median(1200 * np.abs(np.log2(fa[both] / fb[both])))) if both.any() else float("inf")
    return {"corr": corr, "f0_cents": cents, "rms_ratio_db": 20 * np.log10(rms_b / rms_a + 1e-12),
            "aligned_s": len(A) / SR, "dur_out_s": len(b) / SR}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api", default="http://127.0.0.1:18181")
    p.add_argument("--udp", default="127.0.0.1:18101")
    p.add_argument("--input", default=str(ROOT / "input" / "qiqi-espeak-pinyin-zh-en-mixed.wav"))
    p.add_argument("--manifest", default=str(ROOT / "golden_manifest.json"))
    p.add_argument("--tmp", default="/tmp/opencode")
    args = p.parse_args()

    manifest = json.loads(Path(args.manifest).read_text())
    base = Path(args.manifest).resolve().parent
    tmp = Path(args.tmp); tmp.mkdir(parents=True, exist_ok=True)

    failures = []
    print(f"backend: api={args.api} udp={args.udp} input={args.input}")
    for std in manifest["standards"]:
        repro = std.get("repro", {})
        mode = repro.get("mode", "none")
        if mode == "none":
            print(f"[skip] {std['id']}: {repro.get('note', 'no repro spec')}")
            continue
        ref = base / std["output"]
        out = tmp / f"verify_{std['id']}.wav"
        if mode == "file":
            switch_mode(args.api, "file_rvc", repro["model_id"])
            run_file(args.api, repro["model_id"], args.input, out)
        elif mode == "streaming":
            switch_mode(args.api, "rt_rvc", repro["model_id"])
            run_streaming(args.api, args.udp, repro["model_id"], args.input, out)
        m = compare(ref, out)
        ok = (m["corr"] >= repro["corr_min"] and m["f0_cents"] <= repro["f0_max_cents"])
        tag = "PASS" if ok else "FAIL"
        print(f"[{tag}] {std['id']} ({mode}, model={repro['model_id']}): "
              f"corr={m['corr']:.4f}(>={repro['corr_min']}) "
              f"F0={m['f0_cents']:.1f}c(<={repro['f0_max_cents']}) "
              f"rms={m['rms_ratio_db']:+.1f}dB aligned={m['aligned_s']:.1f}s")
        if not ok:
            failures.append(std["id"])

    if failures:
        print("Backend verification FAILED:", ", ".join(failures))
        return 1
    print("Backend verification PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
