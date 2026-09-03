#!/usr/bin/env python3
"""Verify the locked Golden Model output and its exact input context."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

from compare_streaming_quality import compare as compare_streaming_quality


ROOT = Path(__file__).resolve().parent


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_hash(label: str, path: Path, expected: str, failures: list[str]) -> None:
    if not path.is_file():
        failures.append(f"{label}: missing file: {path}")
        return
    actual = sha256(path)
    if actual != expected:
        failures.append(f"{label}: SHA-256 {actual}, expected {expected}")
    else:
        print(f"PASS {label}: {actual}")


def check_runtime(record: dict, failures: list[str]) -> None:
    expected_platform = {
        "python": record["python"],
        "machine": record["machine"],
        "libsndfile": record["libsndfile"],
    }
    actual_platform = {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "libsndfile": sf.__libsndfile_version__,
    }
    if actual_platform != expected_platform:
        failures.append(
            f"runtime platform: {actual_platform}, expected {expected_platform}"
        )
    else:
        print(f"PASS runtime platform: {actual_platform}")

    for package, expected in record["packages"].items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            failures.append(f"runtime package {package}: not installed")
            continue
        if actual != expected:
            failures.append(
                f"runtime package {package}: version {actual}, expected {expected}"
            )
        else:
            print(f"PASS runtime package {package}: {actual}")


def check_rvc_source(record: dict, failures: list[str]) -> None:
    path = Path(record["path"]).expanduser().resolve()
    if not (path / ".git").exists():
        failures.append(f"rvc source: missing git repository: {path}")
        return
    try:
        actual = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        tracked_paths = record["tracked_paths"]
        dirty = subprocess.run(
            ["git", "-C", str(path), "diff", "--quiet", "HEAD", "--", *tracked_paths],
            check=False,
        ).returncode != 0
    except (OSError, subprocess.CalledProcessError) as error:
        failures.append(f"rvc source: git check failed: {error}")
        return
    if actual != record["commit"]:
        failures.append(f"rvc source commit: {actual}, expected {record['commit']}")
    elif dirty:
        failures.append(f"rvc source: tracked paths are modified: {tracked_paths}")
    else:
        print(f"PASS rvc source: {actual} ({', '.join(tracked_paths)})")


def check_output(label: str, standard: dict, base: Path, failures: list[str]) -> None:
    output = (base / standard["output"]).resolve()
    check_hash(f"{label} output", output, standard["output_sha256"], failures)
    if not output.is_file():
        return
    try:
        info = sf.info(output)
        expected = standard["output_format"]
        actual = {
            "sample_rate": info.samplerate,
            "channels": info.channels,
            "subtype": info.subtype,
            "samples": info.frames,
        }
        if actual != expected:
            failures.append(f"{label} output format: {actual}, expected {expected}")
        else:
            print(f"PASS {label} output format: {actual}")
    except Exception as error:
        failures.append(f"{label} output format: {error}")


def check_quality(
    record: dict,
    records_by_id: dict[str, dict],
    base: Path,
    failures: list[str],
) -> None:
    expected = record.get("quality_against")
    if not expected:
        return
    reference_id = expected.get("standard") or expected.get("reference_case")
    reference = records_by_id.get(reference_id)
    if reference is None:
        failures.append(f"{record['id']} quality: unknown reference {reference_id}")
        return
    settings = record["streaming"]
    stream_hop = (
        round(settings["target_seconds"] * 16_000)
        - round(settings["crossfade_seconds"] * 48_000) // 3
    )
    try:
        actual = compare_streaming_quality(
            base / reference["output"], base / record["output"], stream_hop
        )
    except Exception as error:
        failures.append(f"{record['id']} quality comparison: {error}")
        return
    if abs(actual["duration_delta_s"]) > 1e-12:
        failures.append(
            f"{record['id']} quality duration delta: "
            f"{actual['duration_delta_s']:.9f}s"
        )
    checked = {}
    for metric, expected_value in expected.items():
        if metric in {"standard", "reference_case"}:
            continue
        actual_value = actual[metric]
        checked[metric] = actual_value
        if (
            actual_value is None
            or not np.isfinite(actual_value)
            or abs(actual_value - expected_value) > 1e-6
        ):
            failures.append(
                f"{record['id']} quality {metric}: "
                f"{actual_value}, expected {expected_value}"
            )
    if not any(failure.startswith(f"{record['id']} quality") for failure in failures):
        print(f"PASS {record['id']} quality: {checked}")


def check_all_unvoiced_f0(failures: list[str]) -> None:
    from run_streaming_reference import StreamingPipeline

    class ZeroRmvpe:
        @staticmethod
        def infer_from_audio(samples, thred=0.03):
            return np.zeros(200, dtype=np.float32)

    pipeline = object.__new__(StreamingPipeline)
    pipeline.model_rmvpe = ZeroRmvpe()
    coarse, continuous = pipeline.get_f0(
        np.zeros(32_000, dtype=np.float32), 200, 0, "rmvpe"
    )
    if not np.array_equal(coarse, np.ones(200, dtype=np.int32)):
        failures.append("all-unvoiced F0: coarse pitch is not all 1")
    elif not np.array_equal(continuous, np.zeros(200, dtype=np.float32)):
        failures.append("all-unvoiced F0: continuous pitch is not all 0")
    else:
        print("PASS all-unvoiced F0: coarse=1, continuous=0")


def reproduce_standard(
    standard: dict,
    context: dict,
    base: Path,
    model: Path,
    failures: list[str],
) -> None:
    if standard["generator_mode"] not in {"random-vae", "deterministic"}:
        failures.append(
            f"{standard['id']} reproduction: unsupported generator mode "
            f"{standard['generator_mode']!r}"
        )
        return
    if "wav_peak_timestamp" not in standard:
        failures.append(f"{standard['id']} reproduction: missing wav_peak_timestamp")
        return

    produced_by = standard["produced_by"]
    streaming = produced_by.startswith("run_streaming_reference.py")
    runner_record = context["streaming_runner"] if streaming else context["runner"]
    runner = (base / runner_record["path"]).resolve()
    expected = (base / standard["output"]).resolve()
    with tempfile.TemporaryDirectory(prefix="rvc-golden-") as temp:
        temp_dir = Path(temp)
        extraction = standard.get("input_extraction")
        if extraction:
            source = (base / extraction["source"]).resolve()
            input_path = temp_dir / "input-16k.wav"
            extract_command = [
                extraction["executable"],
                "-y",
                "-v", "error",
                "-i", str(source),
                *extraction["arguments"],
                str(input_path),
            ]
            extract_result = subprocess.run(extract_command, check=False)
            if extract_result.returncode != 0:
                failures.append(
                    f"{standard['id']} input extraction: "
                    f"{extraction['executable']} exited {extract_result.returncode}"
                )
                return
            check_hash(
                f"{standard['id']} extracted input",
                input_path,
                extraction["output_sha256"],
                failures,
            )
            if failures:
                return
        else:
            input_path = (base / context["input"]).resolve()
        output = temp_dir / expected.name
        command = [sys.executable, str(runner), "--input", str(input_path),
                   "--model", str(model)]
        if streaming:
            settings = standard["streaming"]
            raw_output = temp_dir / f"{standard['id']}-raw.wav"
            stream_output = raw_output if settings["audition_timeline"] else output
            command.extend([
                "--output", str(stream_output),
                "--tensor-dir", str(temp_dir / "tensors"),
                "--flush-seconds", str(settings["flush_seconds"]),
                "--target-seconds", str(settings["target_seconds"]),
                "--left-context-seconds", str(settings["left_context_seconds"]),
                "--right-context-seconds", str(settings["right_context_seconds"]),
                "--crossfade-seconds", str(settings["crossfade_seconds"]),
            ])
            if settings["audition_timeline"]:
                command.extend(["--audition-output", str(output)])
            if settings["full_history"]:
                command.append("--full-history")
            if settings["reset_generator_rng"]:
                command.append("--reset-generator-rng")
        else:
            command.extend([
                "--output", str(output),
                "--tensor-dir", str(temp_dir / "tensors"),
                "--metadata", str(temp_dir / "reference.json"),
            ])
        command.extend([
            "--seed", str(context["parameters"]["random_seed"]),
            "--wav-peak-timestamp", str(standard["wav_peak_timestamp"]),
        ])
        if standard["generator_mode"] == "deterministic":
            command.append("--deterministic-generator")
        env = os.environ.copy()
        env["RVC_SOURCE"] = context["rvc_source"]["path"]
        env["RVC_CUDA_GRAPH"] = "0"
        print(f"Reproducing {standard['id']} with {runner.name}...", flush=True)
        result = subprocess.run(command, env=env, check=False)
        if result.returncode != 0:
            failures.append(
                f"{standard['id']} reproduction: runner exited {result.returncode}"
            )
            return
        check_hash(
            f"{standard['id']} reproduced output",
            output,
            standard["output_sha256"],
            failures,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(ROOT / "golden_manifest.json"))
    parser.add_argument("--model", default=None,
                        help="override the model path recorded in the manifest")
    parser.add_argument(
        "--reproduce",
        metavar="STANDARD",
        help="rerun a standard and require an exact output SHA-256 match",
    )
    args = parser.parse_args()

    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text())
    base = manifest_path.parent
    failures: list[str] = []

    context = manifest["context"]
    model_record = context["model"]
    model = Path(args.model or model_record["path"]).expanduser().resolve()
    check_hash("input", (base / context["input"]).resolve(),
               context["input_sha256"], failures)
    check_hash("model", model, model_record["sha256"], failures)
    check_hash("runner", (base / context["runner"]["path"]).resolve(),
               context["runner"]["sha256"], failures)
    check_hash("verifier", (base / context["verifier"]["path"]).resolve(),
               context["verifier"]["sha256"], failures)
    check_hash("streaming runner",
               (base / context["streaming_runner"]["path"]).resolve(),
               context["streaming_runner"]["sha256"], failures)
    check_hash("streaming quality comparator",
               (base / context["streaming_quality_comparator"]["path"]).resolve(),
               context["streaming_quality_comparator"]["sha256"], failures)
    check_all_unvoiced_f0(failures)
    check_runtime(context["runtime"], failures)
    check_rvc_source(context["rvc_source"], failures)
    for label, record in context["assets"].items():
        check_hash(f"asset {label}", (base / record["path"]).resolve(),
                   record["sha256"], failures)

    # Each locked standard is verified independently so a drift in any one
    # (audible or numeric, offline or streaming) fails the check.
    all_records = manifest["standards"] + manifest.get("reference_cases", [])
    records_by_id = {record["id"]: record for record in all_records}
    for standard in manifest["standards"]:
        check_output(standard["id"], standard, base, failures)
    for case in manifest.get("reference_cases", []):
        extraction = case["input_extraction"]
        check_hash(
            f"{case['id']} source",
            (base / extraction["source"]).resolve(),
            extraction["source_sha256"],
            failures,
        )
        check_output(case["id"], case, base, failures)
    if not failures:
        for record in all_records:
            check_quality(record, records_by_id, base, failures)

    if args.reproduce and not failures:
        matches = [
            standard for standard in all_records
            if standard["id"] == args.reproduce
        ]
        if not matches:
            failures.append(f"unknown standard for reproduction: {args.reproduce}")
        elif not matches[0]["produced_by"].startswith(
            ("run_reference.py", "run_streaming_reference.py")
        ):
            failures.append(
                f"{args.reproduce} is not produced by a supported reference runner"
            )
        else:
            reproduce_standard(matches[0], context, base, model, failures)

    if failures:
        print("Golden verification FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("Golden verification PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
