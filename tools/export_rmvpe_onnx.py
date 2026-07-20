#!/usr/bin/env python3
"""
导出 RMVPE 音高提取器 → ONNX
用法: python tools/export_rmvpe_onnx.py [--output rmvpe.onnx]
"""
import argparse
import torch
import numpy as np


def export_rmvpe_mel_model(args):
    """导出 RMVPE 内部的 mel → F0 子模型"""
    import sys
    sys.path.insert(0, ".")
    from infer.lib.rmvpe import RMVPE

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[*] Loading RMVPE from {args.rmvpe_path} on {device}...")

    rmvpe = RMVPE(args.rmvpe_path, is_half=False, device=device)
    model = rmvpe.model.eval().to(device)

    print(f"[*] RMVPE model loaded")

    dummy = torch.randn(1, 1024, 128, device=device)
    print(f"[*] Dummy mel input shape: {dummy.shape}")

    torch.onnx.export(
        model,
        dummy,
        args.output,
        input_names=["mel"],
        output_names=["f0"],
        opset_version=args.optset,
    )
    print(f"[✓] Exported to {args.output}")

    import onnx
    onnx.checker.check_model(args.output)
    print(f"[✓] ONNX model validated")

    import onnxruntime
    sess = onnxruntime.InferenceSession(args.output)
    out = sess.get_outputs()[0]
    print(f"[✓] Output shape: {out.shape}")


def main():
    parser = argparse.ArgumentParser(description="Export RMVPE to ONNX")
    parser.add_argument("--rmvpe-path", default="assets/rmvpe/rmvpe.pt",
                        help="Path to rmvpe.pt")
    parser.add_argument("--output", default="rmvpe.onnx",
                        help="Output ONNX file path")
    parser.add_argument("--opset", type=int, default=17,
                        help="ONNX opset version")
    args = parser.parse_args()
    export_rmvpe_mel_model(args)


if __name__ == "__main__":
    main()
