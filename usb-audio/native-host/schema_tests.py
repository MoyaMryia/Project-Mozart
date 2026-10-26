#!/usr/bin/env python3
"""验证原生模块与唯一协议 schema 的字节契约。"""
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

source = Path(__file__).resolve().parent
root = source.parents[1]
schema = json.loads((source.parent / "protocol.json").read_text())
header = (root / "IO/include/mozart/usb_protocol_generated.h").read_text()
definitions = dict(re.findall(r"^#define MOZART_USB_(\w+) ([^\n]+)$", header, re.M))
for key, value in schema["constants"].items():
    if float(definitions[key]) != value:
        raise AssertionError(f"schema mismatch: {key}")
for key, value in schema["header_offsets"].items():
    if int(definitions[key]) != value:
        raise AssertionError(f"offset mismatch: {key}")
if int(definitions["HEADER_BYTES"]) != struct.calcsize(schema["formats"]["HEADER_FORMAT"]):
    raise AssertionError("header size mismatch")
if (source / "include/mozart/usb_protocol_generated.h").exists():
    raise AssertionError("private protocol header shadows IO header")
core, host = sys.argv[1:]
c = schema["constants"]
body = struct.pack(schema["formats"]["HELLO_FORMAT"], c["CONTROL_HELLO"], c["VERSION"],
                   c["CAP_CONTROL"] | c["CAP_FAST64"], c["FAST64_SAMPLES"])
golden = struct.pack(schema["formats"]["HEADER_FORMAT"], c["MAGIC"], c["VERSION"],
                     c["TYPE_CONTROL"], c["STREAM_CONTROL"], 0, 0, 0x12345678,
                     0x0102030405060708, len(body)) + body
actual = bytes.fromhex(subprocess.check_output([core, "--golden"], text=True).strip())
if actual != golden:
    raise AssertionError("native HELLO differs from schema golden")
status = json.loads(subprocess.check_output([core, "--json"], text=True))
if status["reason"] != 'quote" slash\\ newline\n错误😀':
    raise AssertionError("status JSON does not preserve reason")
if status["trace_cleanup_reason"] != "":
    raise AssertionError("status manufactured a trace cleanup failure")
estimate = status["alsa_e2e_queue_estimate_us"]
if estimate != {
    "estimated": True, "physical_measurement": False, "warmup_excluded_first_packets": 100,
    "count": 0, "p50": 0, "p99": 0, "max": 0, "bucket_us": 32,
    "budget_us": 10000, "over_budget_count": 0,
    "max_evidence": {"seq": 0, "capture_frames": 0, "playback_frames": 0, "delta_us": 0},
}:
    raise AssertionError("empty ALSA estimate status differs from contract")
sampled = json.loads(subprocess.check_output([core, "--estimate-json"], text=True))
if sampled["alsa_e2e_queue_estimate_us"] != {
    "estimated": True, "physical_measurement": False, "warmup_excluded_first_packets": 100,
    "count": 2, "p50": 6015, "p99": 13000, "max": 13000, "bucket_us": 32,
    "budget_us": 10000, "over_budget_count": 1,
    "max_evidence": {"seq": 101, "capture_frames": 96, "playback_frames": 480, "delta_us": 1000},
}:
    raise AssertionError("ALSA queue estimate formula, budget or evidence differs from independent expected values")
if sampled["rtt_us"]["count"] != 0:
    raise AssertionError("ALSA queue estimate contaminated USB RTT statistics")
with tempfile.TemporaryDirectory(dir=Path(core).parent, prefix="cli-tests-") as work:
    status_path = Path(work) / "failed.json"
    for arguments in (["--seconds", "-1"], ["--target", "9"], ["--input", "missing.wav"],
                      ["--seconds", "18446744073709551616"], ["--unknown"]):
        status_path.unlink(missing_ok=True)
        result = subprocess.run([host, "--status-file", str(status_path), *arguments],
                                text=True, capture_output=True, timeout=5)
        if result.returncode == 0:
            raise AssertionError(f"invalid CLI returned success: {arguments}")
        if result.returncode != 2 or "invalid arguments:" not in result.stderr or status_path.exists():
            raise AssertionError(f"CLI error did not stop before runtime: {arguments}")
    invalid_input = Path(work) / "invalid.wav"
    invalid_input.write_bytes(b"invalid RIFF WAV data")
    output = Path(work) / "output.wav"
    result = subprocess.run([host, "--status-file", str(status_path), "--input", str(invalid_input),
                             "--output", str(output)], text=True, capture_output=True, timeout=5)
    state = json.loads(status_path.read_text())
    if result.returncode != 1 or state["state"] != "failed" or "RIFF" not in state["reason"]:
        raise AssertionError("runtime input failure lost persistent reason")
    if state["alsa_e2e_queue_estimate_us"]["count"] != 0:
        raise AssertionError("file mode manufactured complete ALSA chain samples")
    before = invalid_input.read_bytes()
    result = subprocess.run([host, "--status-file", str(invalid_input), "--input", str(invalid_input),
                             "--output", str(output)], text=True, capture_output=True, timeout=5)
    if result.returncode != 2 or invalid_input.read_bytes() != before:
        raise AssertionError("artifact path collision overwrote source input")
print("schema, golden, JSON and CLI integration checks passed")
