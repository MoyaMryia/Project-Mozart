#!/usr/bin/env python3
"""导出 RMVPE 音高提取器 → ONNX"""
import argparse
import sys
import torch


def main():
    parser = argparse.ArgumentParser(description="Export RMVPE to ONNX")
    parser.add_argument("--rmvpe-path", default="assets/rmvpe/rmvpe.pt")
    parser.add_argument("--output", default="rmvpe.onnx")
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[*] Loading RMVPE from {args.rmvpe_path} on {device}...")

    sys.path.insert(0, ".")
    from infer.lib.rmvpe import RMVPE

    rmvpe = RMVPE(args.rmvpe_path, is_half=False, device=device)
    model = rmvpe.model.eval().to(device)

    dummy = torch.randn(1, 128, 128, device=device)
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
    print(f"[OK] ONNX model validated")

    import onnxruntime
    import numpy as np
    sess = onnxruntime.InferenceSession(args.output)
    ort_out = sess.run(None, {sess.get_inputs()[0].name: dummy.cpu().numpy()})[0]
    diff = np.abs(ort_out - ref.cpu().numpy()).max()
    print(f"[OK] Max diff: {diff:.6f}")
    assert diff < 1e-3, f"Mismatch: {diff}"
    print(f"[SUCCESS] RMVPE ONNX export done!")


if __name__ == "__main__":
    main()
