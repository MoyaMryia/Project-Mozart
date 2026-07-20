#!/usr/bin/env python3
"""
导出 HuBERT 特征提取器 → ONNX
用法: python tools/export_hubert_onnx.py [--output hubert_base.onnx]
"""
import argparse
import torch
import fairseq
from fairseq import checkpoint_utils


def main():
    parser = argparse.ArgumentParser(description="Export HuBERT to ONNX")
    parser.add_argument("--hubert-path", default="assets/hubert/hubert_base.pt",
                        help="Path to hubert_base.pt")
    parser.add_argument("--output", default="hubert_base.onnx",
                        help="Output ONNX file path")
    parser.add_argument("--opset", type=int, default=17,
                        help="ONNX opset version")
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[*] Loading HuBERT from {args.hubert_path} on {device}...")

    models, _, _ = checkpoint_utils.load_model_ensemble_and_task(
        [args.hubert_path], suffix=""
    )
    model = models[0].eval().to(device)

    print(f"[*] Model loaded: {type(model).__name__}")

    dummy = torch.randn(1, 16000 * 10, device=device)
    print(f"[*] Dummy input shape: {dummy.shape}")

    torch.onnx.export(
        model,
        dummy,
        args.output,
        input_names=["audio"],
        output_names=["features"],
        dynamic_axes={"audio": {1: "audio_len"}},
        opset_version=args.opset,
    )
    print(f"[✓] Exported to {args.output}")

    import onnx
    onnx.checker.check_model(args.output)
    print(f"[✓] ONNX model validated")

    import onnxruntime
    sess = onnxruntime.InferenceSession(args.output)
    out = sess.get_outputs()[0]
    print(f"[✓] Output shape: {out.shape}")


if __name__ == "__main__":
    main()
