#!/usr/bin/env python3
"""
导出 RVC Generator (net_g) → ONNX
用法: python tools/export_generator_onnx.py de_narrator.pth
      python tools/export_generator_onnx.py --all  # 批量导出所有 .pth
"""
import argparse
import sys
import torch
from pathlib import Path


def get_rvc_config():
    """从 RVC WebUI config 加载配置"""
    sys.path.insert(0, ".")
    from configs.config import Config
    return Config()


def load_net_g(model_name, device="cuda"):
    """加载 RVC 音色模型的 net_g"""
    from infer.modules.vc.modules import VC
    config = get_rvc_config()
    vc = VC(config)
    vc.get_vc(model_name)
    net_g = vc.net_g.eval().to(device)
    return net_g, vc


def export_model(model_name, output_path, device, opset):
    """导出单个音色模型为 ONNX"""
    print(f"\n{'='*60}")
    print(f"[*] Exporting: {model_name}")
    print(f"{'='*60}")

    net_g, vc = load_net_g(model_name, device)
    tgt_sr = vc.tgt_sr if hasattr(vc, 'tgt_sr') else 40000

    print(f"[*] net_g type: {type(net_g).__name__}")
    print(f"[*] target sample rate: {tgt_sr}")

    T = 200
    feats = torch.randn(1, T, 768, device=device)
    p_len = torch.tensor([T], device=device).long()
    pitch = torch.randint(0, 400, (1, T), device=device).long()
    pitchf = torch.randn(1, T, device=device)
    sid = torch.tensor([0], device=device).long()

    print(f"[*] Dummy inputs: feats={list(feats.shape)}, p_len={p_len}, "
          f"pitch={list(pitch.shape)}, pitchf={list(pitchf.shape)}, sid={sid}")

    torch.onnx.export(
        net_g,
        (feats, p_len, pitch, pitchf, sid),
        output_path,
        input_names=["feats", "p_len", "pitch", "pitchf", "sid"],
        output_names=["audio"],
        dynamic_axes={
            "feats": {1: "T"},
            "pitch": {1: "T"},
            "pitchf": {1: "T"},
            "audio": {2: "audio_len"},
        },
        opset_version=opset,
    )
    print(f"[✓] Exported to {output_path}")

    import onnx
    onnx.checker.check_model(str(output_path))
    print(f"[✓] ONNX model validated")

    import onnxruntime
    sess = onnxruntime.InferenceSession(str(output_path))
    out = sess.get_outputs()[0]
    print(f"[✓] Output shape: {out.shape}")


def main():
    parser = argparse.ArgumentParser(description="Export RVC Generator to ONNX")
    parser.add_argument("model", nargs="*",
                        help="Model .pth file(s) to export")
    parser.add_argument("--all", action="store_true",
                        help="Export all .pth files in assets/weights/")
    parser.add_argument("--weights-dir", default="assets/weights",
                        help="Directory containing .pth models")
    parser.add_argument("--output-dir", default=".",
                        help="Output directory for .onnx files")
    parser.add_argument("--device", default="cuda",
                        help="Device: cuda or cpu")
    parser.add_argument("--opset", type=int, default=17,
                        help="ONNX opset version")
    args = parser.parse_args()

    device = args.device
    if device == "cuda" and not torch.cuda.is_available():
        print("[!] CUDA not available, falling back to CPU")
        device = "cpu"

    models_to_export = []

    if args.all:
        weights_dir = Path(args.weights_dir)
        if not weights_dir.exists():
            print(f"[!] Weights directory not found: {weights_dir}")
            sys.exit(1)
        models_to_export = sorted(weights_dir.glob("*.pth"))
        print(f"[*] Found {len(models_to_export)} .pth files in {weights_dir}")
    elif args.model:
        for m in args.model:
            p = Path(m)
            if p.suffix != ".pth":
                p = p.with_suffix(".pth")
            models_to_export.append(p)
    else:
        print("[!] Specify at least one model file or use --all")
        print("    e.g.: python tools/export_generator_onnx.py de_narrator.pth")
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for model_path in models_to_export:
        if not model_path.exists():
            print(f"[!] Skipping missing file: {model_path}")
            continue
        onnx_name = model_path.stem + ".onnx"
        output_path = output_dir / onnx_name
        export_model(str(model_path), output_path, device, args.opset)

    print(f"\n[✓] Done! Exported {len(models_to_export)} model(s)")
    print(f"[*] Output files in: {output_dir.resolve()}")
    for m in models_to_export:
        onnx_name = Path(m).stem + ".onnx"
        print(f"    {output_dir / onnx_name}")


if __name__ == "__main__":
    main()
