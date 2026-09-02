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

import soundfile as sf


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


def reproduce_offline_standard(
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

    runner = (base / context["runner"]["path"]).resolve()
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
        command = [
            sys.executable,
            str(runner),
            "--input", str(input_path),
            "--model", str(model),
            "--output", str(output),
            "--tensor-dir", str(temp_dir / "tensors"),
            "--metadata", str(temp_dir / "reference.json"),
            "--seed", str(context["parameters"]["random_seed"]),
            "--wav-peak-timestamp", str(standard["wav_peak_timestamp"]),
        ]
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
        help="rerun an offline standard and require an exact output SHA-256 match",
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
    check_runtime(context["runtime"], failures)
    check_rvc_source(context["rvc_source"], failures)
    for label, record in context["assets"].items():
        check_hash(f"asset {label}", (base / record["path"]).resolve(),
                   record["sha256"], failures)

    # Each locked standard is verified independently so a drift in any one
    # (audible or numeric, offline or streaming) fails the check.
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

    if args.reproduce and not failures:
        reproducible = manifest["standards"] + manifest.get("reference_cases", [])
        matches = [
            standard for standard in reproducible
            if standard["id"] == args.reproduce
        ]
        if not matches:
            failures.append(f"unknown standard for reproduction: {args.reproduce}")
        elif not matches[0]["produced_by"].startswith("run_reference.py"):
            failures.append(
                f"{args.reproduce} is not produced by the offline reference runner"
            )
        else:
            reproduce_offline_standard(matches[0], context, base, model, failures)

    if failures:
        print("Golden verification FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("Golden verification PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
