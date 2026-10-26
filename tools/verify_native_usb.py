#!/usr/bin/env python3
"""离线验收 native USB trace、停止状态和当前 C DSP 真值。"""

import argparse
import csv
import ctypes
import json
import math
from pathlib import Path
import sys
import wave

import numpy as np


PROTOCOL = json.loads(
    (Path(__file__).resolve().parents[1] / "usb-audio/protocol.json").read_text()
)["constants"]
DEFAULT_STEADY_BUDGET_MS = PROTOCOL["DIGITAL_LATENCY_BUDGET_US"] / 1000
MAX_ERROR_LSB = 1
# 当前 native-host/runtime.hpp 的直方图序列化契约；测试检查两端一致。
STATUS_HISTOGRAM_BINS = 4096
CSV_FIELDS = ("direction", "seq", "host_us", "wire_timestamp_us", "rtt_us", "source_frames")


class ArtifactError(ValueError):
    """输入产物格式或参考库接口错误。"""


def check_alsa_estimate(status, errors):
    metric = status.get("alsa_e2e_queue_estimate_us")
    if metric is None:
        if status["source_kind"] == "alsa":
            errors.append("ALSA 完整队列估算缺失")
        return
    if not isinstance(metric, dict):
        raise ArtifactError("ALSA 完整队列估算必须是对象")
    for key in ("count", "p50", "p99", "max", "bucket_us", "budget_us", "over_budget_count"):
        uint(metric.get(key), f"alsa_e2e_queue_estimate_us.{key}")
    if metric.get("estimated") is not True or metric.get("physical_measurement") is not False:
        errors.append("ALSA 估算类型标记错误")
    if metric["budget_us"] != PROTOCOL["DIGITAL_LATENCY_BUDGET_US"]:
        errors.append("ALSA 完整队列预算与协议定义不一致")
    expected_count = status["played"] if status["source_kind"] == "alsa" else 0
    if metric["count"] != expected_count:
        errors.append("ALSA 完整队列估算未覆盖全部稳态播放包")
    if not 0 <= metric["p50"] <= metric["p99"] <= metric["max"]:
        errors.append("ALSA 完整队列估算分位数顺序错误")
    if metric["over_budget_count"] or metric["max"] > metric["budget_us"]:
        errors.append("ALSA 完整队列估算超预算")
    if metric["count"]:
        evidence = metric.get("max_evidence")
        if not isinstance(evidence, dict):
            raise ArtifactError("ALSA 最大值证据必须是对象")
        for key in ("seq", "capture_frames", "playback_frames", "delta_us"):
            uint(evidence.get(key), f"alsa_e2e_queue_estimate_us.max_evidence.{key}")
        expected = evidence["delta_us"] + (evidence["capture_frames"] + evidence["playback_frames"]) * 1000000 // PROTOCOL["RATE"]
        if expected != metric["max"]:
            errors.append("ALSA 完整队列最大值与独立公式不一致")
        if not status["warmup"] <= evidence["seq"] < status["rx"]:
            errors.append("ALSA 最大值证据包不属于稳态播放范围")


def uint(value, name, bits=64):
    if type(value) is not int or value < 0 or value.bit_length() > bits:
        raise ArtifactError(f"{name} 必须是 {bits} 位无符号整数")
    return value


def invalid_json_constant(value):
    raise ArtifactError(f"JSON 含非有限常量 {value}")


def json_integer(value):
    try:
        return int(value)
    except ValueError as exc:
        raise ArtifactError("JSON 整数字段超出转换位数范围") from exc


def check_json_text(value):
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, child in value.items():
            check_json_text(key)
            check_json_text(child)
    elif isinstance(value, list):
        for child in value:
            check_json_text(child)


def json_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ArtifactError(f"JSON 数值超出有限范围：{value}")
    return number


def load_status(path):
    status = json.loads(path.read_text(), parse_constant=invalid_json_constant,
                        parse_float=json_float, parse_int=json_integer)
    check_json_text(status)
    if not isinstance(status, dict):
        raise ArtifactError("status 必须是对象")
    for key in ("tx", "rx", "played", "warmup", "source_frames", "output_frames", "block"):
        uint(status.get(key), f"status.{key}")
    if type(status.get("negotiated")) is not bool:
        raise ArtifactError("status.negotiated 必须是布尔值")
    if not isinstance(status.get("state"), str):
        raise ArtifactError("status.state 必须是字符串")
    expected_block = PROTOCOL["FAST64_SAMPLES"] if status["negotiated"] else PROTOCOL["FAST_SAMPLES"]
    if status["block"] != expected_block:
        raise ArtifactError("status.block 与 negotiated 不一致")
    histogram = status.get("rtt_us")
    if not isinstance(histogram, dict):
        raise ArtifactError("status.rtt_us 必须是对象")
    for key in ("count", "p50", "p99", "max", "bucket_us"):
        uint(histogram.get(key), f"status.rtt_us.{key}")
    if not histogram["bucket_us"]:
        raise ArtifactError("status.rtt_us.bucket_us 必须大于零")
    for key in ("source_total_frames", "padding_frames", "stream"):
        if key in status:
            uint(status[key], f"status.{key}")
    # host 对实时采集使用 source_total_frames=0；文件输入记录完整文件长度。
    status["source_kind"] = "file" if status.get("source_total_frames", 0) else "alsa"
    xrun = status.get("xrun")
    if not isinstance(xrun, dict):
        raise ArtifactError("status.xrun 必须是对象")
    for key in ("capture_overrun", "playback_underrun", "capture_suspend", "playback_suspend"):
        if key not in xrun:
            raise ArtifactError(f"status.xrun 缺少 {key}")
    for key, value in xrun.items():
        uint(value, f"status.xrun.{key}")
    return status


def load_packets(path):
    packets = {"tx": [], "rx": []}
    with path.open(newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != list(CSV_FIELDS):
            raise ArtifactError(f"CSV schema 必须是 {','.join(CSV_FIELDS)}")
        for line, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ArtifactError(f"CSV 第 {line} 行字段数错误")
            direction = row["direction"]
            if direction not in packets:
                raise ArtifactError(f"CSV 第 {line} 行 direction 错误")
            parsed = {"line": line}
            for key in CSV_FIELDS[1:]:
                if not row[key].isascii() or not row[key].isdecimal():
                    raise ArtifactError(f"CSV 第 {line} 行 {key} 必须是非负整数")
                bits = 32 if key == "seq" else 64
                if len(row[key]) > len(str((1 << bits) - 1)):
                    raise ArtifactError(f"CSV 第 {line} 行 {key} 超出 {bits} 位整数范围")
                parsed[key] = uint(int(row[key]), f"CSV 第 {line} 行 {key}", bits)
            packets[direction].append(parsed)
    return packets


def load_wav(path):
    with wave.open(str(path), "rb") as source:
        expected = (PROTOCOL["RATE"], PROTOCOL["CHANNELS"], PROTOCOL["SAMPLE_BYTES"], "NONE")
        actual = (source.getframerate(), source.getnchannels(), source.getsampwidth(), source.getcomptype())
        if actual != expected:
            raise ArtifactError(f"{path.name} 必须是 48 kHz 双声道 S16 PCM WAV")
        frames = source.getnframes()
        pcm = source.readframes(frames + 1)
    if len(pcm) != frames * PROTOCOL["CHANNELS"] * PROTOCOL["SAMPLE_BYTES"]:
        raise ArtifactError(f"{path.name} PCM 数据截短")
    if not frames:
        raise ArtifactError(f"{path.name} 为空")
    return np.frombuffer(pcm, dtype="<i2").reshape(frames, PROTOCOL["CHANNELS"])


def statistics(rows, budget_us):
    values = sorted(row["rtt_us"] for row in rows)
    if not values:
        return {"count": 0, "mean_us": None, "max_us": None, "p50_us": None,
                "p95_us": None, "p99_us": None, "max_packets": [], "over_budget": []}
    return {
        "count": len(values), "mean_us": sum(values) / len(values), "max_us": values[-1],
        **{f"p{p}_us": values[(len(values) * p + 99) // 100 - 1] for p in (50, 95, 99)},
        "max_packets": [row for row in rows if row["rtt_us"] == values[-1]],
        "over_budget": [row for row in rows if row["rtt_us"] > budget_us],
    }


def nonzero_faults(value, path="status"):
    faults = []
    if isinstance(value, dict):
        for key, child in value.items():
            name = f"{path}.{key}"
            normalized = key.lower().replace("_", "")
            if normalized.startswith("bad") or path == "status.xrun":
                if path != "status.xrun":
                    uint(child, name)
                if child:
                    faults.append({"field": name, "value": child})
            else:
                faults.extend(nonzero_faults(child, name))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            faults.extend(nonzero_faults(child, f"{path}[{index}]"))
    return faults


def check_trace(status, packets, budget_us, errors):
    tx, rx = packets["tx"], packets["rx"]
    block = status["block"]
    all_stats = statistics(rx, budget_us)
    steady = statistics([row for row in rx if row["seq"] >= status["warmup"]], budget_us)
    report = {"tx_count": len(tx), "rx_count": len(rx), "all": all_stats, "steady": steady,
              "source_frames": sum(row["source_frames"] for row in tx)}
    for direction, rows in packets.items():
        previous = None
        for index, row in enumerate(rows):
            tag = f"{direction} line={row['line']} seq={row['seq']}"
            if row["seq"] != index:
                errors.append(f"{tag}: 序号不连续，期望 {index}")
            if previous is not None and row["host_us"] < previous:
                errors.append(f"{tag}: 本机时间倒退")
            previous = row["host_us"]
            if not 0 < row["source_frames"] <= block:
                errors.append(f"{tag}: source_frames 超出块范围")
            if index < len(rows) - 1 and row["source_frames"] != block:
                errors.append(f"{tag}: 非末包被截短")
            if direction == "tx" and (row["wire_timestamp_us"] != row["host_us"] or row["rtt_us"] != 0):
                errors.append(f"{tag}: TX 本机时间或 RTT 错误")
    if len(tx) != len(rx) or not tx:
        errors.append("TX/RX 数量不匹配或为空")
    for sent, received in zip(tx, rx):
        tag = f"rx line={received['line']} seq={received['seq']}"
        if sent["seq"] != received["seq"] or sent["source_frames"] != received["source_frames"]:
            errors.append(f"{tag}: TX/RX 对应错误")
        elapsed = received["host_us"] - sent["host_us"]
        if elapsed < 0 or received["rtt_us"] != elapsed:
            errors.append(f"{tag}: 本机时戳 RTT 错误")
        if status["negotiated"] and received["wire_timestamp_us"] != sent["wire_timestamp_us"]:
            errors.append(f"{tag}: wire echo timestamp 错误")
    if not steady["count"]:
        errors.append("稳态 RX 样本为空")
    if steady["over_budget"]:
        errors.append(f"稳态 RTT 超预算，共 {len(steady['over_budget'])} 包")
    expected = {"tx": len(tx), "rx": len(rx), "played": max(0, len(rx) - status["warmup"]),
                "source_frames": report["source_frames"]}
    for key, value in expected.items():
        if status[key] != value:
            errors.append(f"status.{key}={status[key]}，CSV 期望 {value}")
    if status["warmup"] > len(rx):
        errors.append("status.warmup 大于 RX 数量")
    histogram = status["rtt_us"]
    if histogram["count"] != len(rx) or histogram["max"] != all_stats["max_us"]:
        errors.append("status.rtt_us count/max 与全部 RX 不一致")
    # 状态分位使用桶上沿；独立摘要直接使用原始 RTT 的 nearest-rank。
    for p in (50, 99):
        exact = all_stats[f"p{p}_us"]
        reported = histogram[f"p{p}"]
        if exact is not None:
            bucket = exact // histogram["bucket_us"]
            expected_percentile = histogram["max"] if bucket >= STATUS_HISTOGRAM_BINS - 1 else min(
                histogram["max"], (bucket + 1) * histogram["bucket_us"] - 1)
            if reported != expected_percentile:
                errors.append(f"status.rtt_us.p{p} 与全部 RX 的直方图不一致")
    if "padding_frames" in status and status["padding_frames"] != len(tx) * block - report["source_frames"]:
        errors.append("status.padding_frames 与 CSV 不一致")
    if status["source_kind"] == "file" and status["source_total_frames"] < report["source_frames"]:
        errors.append("status.source_total_frames 小于已发送源长度")
    if "stream" in status:
        stream = PROTOCOL["STREAM_FAST64"] if status["negotiated"] else PROTOCOL["STREAM_FAST"]
        if status["stream"] != stream:
            errors.append("status.stream 与 negotiated 不一致")
    return report


class DspConfig(ctypes.Structure):
    _fields_ = [(name, ctypes.c_bool) for name in ("denoise", "gate", "agc", "limiter")]


def reference_pcm(library, source, block):
    """完整源按 USB 块输入当前 DSP，补零排空后裁到原始长度。"""
    try:
        lib = ctypes.CDLL(str(library))
        new, enhance, free = lib.mozart_dsp_new, lib.mozart_dsp_enhance, lib.mozart_dsp_free
    except (OSError, AttributeError) as exc:
        raise ArtifactError(f"参考库加载失败：{exc}") from exc
    pointer = ctypes.POINTER(ctypes.c_float)
    new.argtypes, new.restype = [ctypes.POINTER(DspConfig)], ctypes.c_void_p
    enhance.argtypes = [ctypes.c_void_p, pointer, ctypes.c_size_t, pointer, ctypes.c_size_t]
    enhance.restype = ctypes.c_size_t
    free.argtypes, free.restype = [ctypes.c_void_p], None
    dsp = new(ctypes.byref(DspConfig(True, False, False, False)))
    if not dsp:
        raise ArtifactError("参考 DSP 创建失败")
    # 一秒缓冲容纳启动期积压；一秒补零上限用于发现接口停滞。
    capacity = PROTOCOL["RATE"]
    out = np.empty(capacity, dtype=np.float32)
    zero = np.zeros(block, dtype=np.float32)
    result = []
    emitted = 0

    def push(samples):
        count = enhance(dsp, samples.ctypes.data_as(pointer), block,
                        out.ctypes.data_as(pointer), capacity)
        if count > capacity:
            raise ArtifactError(f"参考 DSP 返回非法输出数量 {count}")
        if not np.isfinite(out[:count]).all():
            raise ArtifactError("参考 DSP 输出含非有限数值")
        result.append(out[:count].copy())
        return count

    try:
        for start in range(0, len(source), block):
            samples = zero.copy()
            valid = min(block, len(source) - start)
            samples[:valid] = source[start:start + valid, 0].astype(np.float32) / np.float32(32768)
            emitted += push(samples)
        padded = 0
        while emitted < len(source) and padded < capacity:
            emitted += push(zero)
            padded += block
        if emitted < len(source):
            raise ArtifactError("参考 DSP 补零一秒后仍未覆盖完整源")
    finally:
        free(dsp)
    mono = np.concatenate(result)[:len(source)]
    return (np.clip(mono, -1, 1) * np.float32(32767)).astype(np.int16)


def verify(trace_dir, status_path, reference_lib, budget_ms=DEFAULT_STEADY_BUDGET_MS):
    errors = []
    summary = {"passed": False, "errors": errors, "steady_budget_ms": budget_ms,
               "max_error_lsb_allowed": MAX_ERROR_LSB,
               "inputs": {"trace_dir": str(trace_dir), "status": str(status_path),
                          "reference_lib": str(reference_lib)},
               "status": None, "trace": None, "audio": None}
    if not math.isfinite(budget_ms) or budget_ms <= 0:
        errors.append("稳态预算必须是有限正数")
        if not math.isfinite(budget_ms):
            summary["steady_budget_ms"] = None
        return summary
    # 各产物独立读取，格式错误写入摘要；内部程序错误继续向上抛出。
    artifacts = {}
    loaders = {
        "status": lambda: load_status(status_path),
        "packets": lambda: load_packets(trace_dir / "packets.csv"),
        "capture": lambda: load_wav(trace_dir / "capture.wav"),
        "response": lambda: load_wav(trace_dir / "response.wav"),
    }
    for key, loader in loaders.items():
        try:
            artifacts[key] = loader()
        except (ArtifactError, OSError, UnicodeError, json.JSONDecodeError, csv.Error, wave.Error, EOFError) as exc:
            errors.append(f"{key}: {exc}")
    status = artifacts.get("status")
    if status is not None:
        summary["status"] = status
        if status["state"] != "stopped":
            errors.append(f"status.state={status['state']}，要求 stopped；reason={status.get('reason')}")
        try:
            summary["faults"] = nonzero_faults(status)
        except ArtifactError as exc:
            errors.append(str(exc))
        else:
            if summary["faults"]:
                errors.append("status 含非零 badcount 或 xrun")
            if status["warmup"] != min(PROTOCOL["NATIVE_WARMUP_PACKETS"], status["rx"]):
                errors.append("status.warmup 与生产预热契约不一致")
            try:
                check_alsa_estimate(status, errors)
            except ArtifactError as exc:
                errors.append(str(exc))
        if "packets" in artifacts:
            summary["trace"] = check_trace(status, artifacts["packets"], budget_ms * 1000, errors)
    if "capture" in artifacts and "response" in artifacts:
        source, actual = artifacts["capture"], artifacts["response"]
        audio = summary["audio"] = {"source_frames": len(source), "output_frames": len(actual)}
        if len(actual) != len(source):
            errors.append("capture/response WAV 长度不一致")
        if status is not None:
            # trace 始终包含预热；ALSA 播放计数只包含预热后的原始采样。
            played_frames = len(actual)
            if status["source_kind"] == "alsa":
                played_frames -= min(played_frames, status["warmup"] * status["block"])
            for key, expected in (("source_frames", len(source)), ("output_frames", played_frames)):
                if status[key] != expected:
                    errors.append(f"status.{key} 与 WAV 长度不一致")
            if status["source_kind"] == "file" and status["output_frames"] != status["source_frames"]:
                errors.append("文件输出未覆盖包含预热的完整源")
            try:
                expected = reference_pcm(reference_lib, source, status["block"])
            except ArtifactError as exc:
                errors.append(str(exc))
            else:
                if len(actual) == len(source):
                    difference = np.abs(actual.astype(np.int32) - expected.astype(np.int32)[:, None])
                    worst = int(difference.max())
                    audio.update({"compared_samples": int(difference.size), "max_error_lsb": worst,
                                  "bad_sample_count": int(np.count_nonzero(difference > MAX_ERROR_LSB)),
                                  "max_error_first_frame_channel": list(map(int, np.unravel_index(difference.argmax(), difference.shape)))})
                    if worst > MAX_ERROR_LSB:
                        errors.append(f"C DSP 逐样本误差 {worst} LSB 超过 {MAX_ERROR_LSB} LSB")
    summary["passed"] = not errors
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--reference-lib", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--steady-budget-ms", type=float, default=DEFAULT_STEADY_BUDGET_MS,
                        help="显式指定稳态 RTT 预算，单位 ms")
    args = parser.parse_args(argv)
    inputs = [args.status, args.reference_lib, *(args.trace_dir / name for name in ("packets.csv", "capture.wav", "response.wav"))]
    if args.output.resolve() in [path.resolve() for path in inputs]:
        parser.error("--output 必须与输入产物路径不同")
    summary = verify(args.trace_dir, args.status, args.reference_lib, args.steady_budget_ms)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(f"{'PASS' if summary['passed'] else 'FAIL'}: {args.output}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
