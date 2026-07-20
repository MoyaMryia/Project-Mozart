#!/usr/bin/env python3
"""
导出 HuBERT 特征提取器 → ONNX
要求: Linux (torch.compile 不支持 Windows)
用法: python tools/export_hubert_onnx.py [--output hubert_base.onnx]
"""
import argparse
import sys
import torch
from fairseq import checkpoint_utils


def export_via_dynamo(wrapper, dummy, output_path):
    """PyTorch >= 2.1 dynamo export (Linux only)"""
    exported = torch.onnx.dynamo_export(wrapper, dummy)
    exported.save(output_path)


def export_via_trace(wrapper, dummy, output_path, opset):
    """Standard jit trace (fallback, may fail on complex models)"""
    torch.onnx.export(
        wrapper, dummy, output_path,
        input_names=["audio"], output_names=["features"],
        dynamic_axes={"audio": {1: "audio_len"}},
        opset_version=opset,
    )


def main():
    parser = argparse.ArgumentParser(description="Export HuBERT to ONNX")
    parser.add_argument("--hubert-path", default="assets/hubert/hubert_base.pt")
    parser.add_argument("--output", default="hubert_base.onnx")
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[*] Loading HuBERT from {args.hubert_path} on {device}...")

    models, _, _ = checkpoint_utils.load_model_ensemble_and_task(
        [args.hubert_path], suffix=""
    )
    model = models[0].eval().to(device)

    class HuBERTTrace(torch.nn.Module):
        def forward(self, x):
            return model(x, padding_mask=None, mask=False,
                         features_only=True, output_layer=9)["x"]

    wrapper = HuBERTTrace().eval().to(device)
    dummy = torch.randn(1, 16000 * 10, device=device)
    print(f"[*] Dummy input: {dummy.shape}")

    with torch.no_grad():
        ref = wrapper(dummy)
        print(f"[*] Ref output: {ref.shape}")

    if sys.platform == "linux":
        print("[*] Exporting via dynamo_export (Linux)...")
        export_via_dynamo(wrapper, dummy, args.output)
    else:
        print("[*] Exporting via jit trace (non-Linux fallback)...")
        print("[!] This may fail on complex models. Run on Linux for best results.")
        export_via_trace(wrapper, dummy, args.output, args.opset)

    print(f"[OK] ONNX saved to {args.output}")

    import onnx
    onnx.checker.check_model(args.output)
    print(f"[OK] ONNX model validated")

    import onnxruntime
    import numpy as np
    sess = onnxruntime.InferenceSession(args.output)
    ort_out = sess.run(None, {sess.get_inputs()[0].name: dummy.cpu().numpy()})[0]
    diff = np.abs(ort_out - ref.cpu().numpy()).max()
    print(f"[OK] PyTorch vs ONNX max diff: {diff:.6f}")
    assert diff < 1e-3, f"Output mismatch! diff={diff}"
    print(f"[OKOKOK] HuBERT ONNX export SUCCESS!")


if __name__ == "__main__":
    main()
