#!/usr/bin/env python3
"""Export the fixed-shape RMVPE network to ONNX."""
import argparse
import sys
from pathlib import Path

import torch


def main():
    parser = argparse.ArgumentParser(description="Export RMVPE to ONNX")
    parser.add_argument("--rmvpe-path", default="assets/rmvpe/rmvpe.pt")
    parser.add_argument("--output", default="rmvpe.onnx")
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--frames", type=int, default=128)
    parser.add_argument(
        "--rvc-source",
        type=Path,
        default=Path("/home/moyamryia/mozart-archive/RVC"),
    )
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    if args.frames <= 0 or args.frames % 32 != 0:
        raise ValueError("frames must be a positive multiple of 32")

    device = torch.device(args.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is unavailable")
    print(f"[*] Loading RMVPE from {args.rmvpe_path} on {device}...")

    sys.path.insert(0, str(args.rvc_source.resolve()))
    from infer.rmvpe import RMVPE

    rmvpe = RMVPE(args.rmvpe_path, is_half=False, device=device)
    model = rmvpe.model.eval().to(device)

    dummy = torch.randn(1, 128, args.frames, device=device)
    print(f"[*] Dummy mel input: {dummy.shape}")

    with torch.no_grad():
        ref = model(dummy)
        print(f"[*] Ref output: {ref.shape}")

    torch.onnx.export(
        model, dummy, args.output,
        input_names=["mel"], output_names=["f0"],
        opset_version=args.opset,
    )
    print(f"[OK] ONNX saved to {args.output}")

    import onnx
    onnx.checker.check_model(args.output)
    print("[OK] ONNX model validated")

    import onnxruntime
    import numpy as np
    sess = onnxruntime.InferenceSession(args.output)
    ort_out = sess.run(None, {sess.get_inputs()[0].name: dummy.cpu().numpy()})[0]
    diff = np.abs(ort_out - ref.cpu().numpy()).max()
    print(f"[OK] Max diff: {diff:.6f}")
    assert diff < 1e-3, f"Mismatch: {diff}"
    print("[SUCCESS] RMVPE ONNX export done!")


if __name__ == "__main__":
    main()
