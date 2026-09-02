#!/usr/bin/env python3
"""Verify the locked Golden Model output and its exact input context."""

import argparse
import hashlib
import json
import subprocess
import sys
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
        dirty = subprocess.run(
            ["git", "-C", str(path), "diff", "--quiet", "HEAD", "--", record["tracked_subtree"]],
            check=False,
        ).returncode != 0
    except (OSError, subprocess.CalledProcessError) as error:
        failures.append(f"rvc source: git check failed: {error}")
        return
    if actual != record["commit"]:
        failures.append(f"rvc source commit: {actual}, expected {record['commit']}")
    elif dirty:
        failures.append(f"rvc source: tracked files under {record['tracked_subtree']} are modified")
    else:
        print(f"PASS rvc source: {actual} ({record['tracked_subtree']})")


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(ROOT / "golden_manifest.json"))
    parser.add_argument("--model", default=None,
                        help="override the model path recorded in the manifest")
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
    check_hash("streaming runner",
               (base / context["streaming_runner"]["path"]).resolve(),
               context["streaming_runner"]["sha256"], failures)
    check_rvc_source(context["rvc_source"], failures)
    for label, record in context["assets"].items():
        check_hash(f"asset {label}", (base / record["path"]).resolve(),
                   record["sha256"], failures)

    # Each locked standard is verified independently so a drift in any one
    # (audible or numeric, offline or streaming) fails the check.
    for standard in manifest["standards"]:
        check_output(standard["id"], standard, base, failures)

    if failures:
        print("Golden verification FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("Golden verification PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
