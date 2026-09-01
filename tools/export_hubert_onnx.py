#!/usr/bin/env python3
"""Export the fixed-window RVC v2 HuBERT feature extractor to ONNX."""

import argparse
from pathlib import Path

import numpy as np
import torch
from transformers import HubertModel


class RvcV2Hubert(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, audio):
        return self.model(
            input_values=audio,
            attention_mask=None,
            output_hidden_states=False,
            return_dict=False,
        )[0]


def main():
    parser = argparse.ArgumentParser(description="Export RVC v2 HuBERT to ONNX")
    parser.add_argument("--hubert-path", default="rvc-golden/assets/hubert_base")
    parser.add_argument("--output", default="hubert_base.onnx")
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument(
        "--dynamic",
        action="store_true",
        help="Export a dynamic-length audio axis instead of the fixed 32000-sample window",
    )
    args = parser.parse_args()

    model = HubertModel.from_pretrained(
        Path(args.hubert_path), local_files_only=True, torch_dtype=torch.float32
    ).eval()
    wrapper = RvcV2Hubert(model).eval()
    dummy = torch.randn(1, 32000)

    with torch.inference_mode():
        reference = wrapper(dummy).numpy()

    dynamic_axes = None
    if args.dynamic:
        dynamic_axes = {
            "audio": {1: "samples"},
            "features": {1: "frames"},
        }

    torch.onnx.export(
        wrapper,
        dummy,
        args.output,
        input_names=["audio"],
        output_names=["features"],
        opset_version=args.opset,
        dynamic_axes=dynamic_axes,
        dynamo=False,
    )

    import onnx
    import onnxruntime

    onnx.checker.check_model(args.output)
    session = onnxruntime.InferenceSession(args.output, providers=["CPUExecutionProvider"])
    actual = session.run(["features"], {"audio": dummy.numpy()})[0]
    max_error = float(np.max(np.abs(actual - reference)))
    if max_error >= 1e-3:
        raise RuntimeError(f"HuBERT ONNX output mismatch: max error {max_error}")
    print(f"Exported {args.output}: shape={actual.shape}, max_error={max_error:.6g}")

    if args.dynamic:
        # A dynamic export is only usable when several lengths actually pass
        # inference with PyTorch-equivalent output.
        for length in (16000, 48000):
            probe = torch.randn(1, length)
            with torch.inference_mode():
                expected = wrapper(probe).numpy()
            produced = session.run(["features"], {"audio": probe.numpy()})[0]
            if produced.shape != expected.shape:
                raise RuntimeError(
                    f"dynamic HuBERT shape mismatch at {length}: {produced.shape} vs {expected.shape}"
                )
            probe_error = float(np.max(np.abs(produced - expected)))
            if probe_error >= 1e-3:
                raise RuntimeError(
                    f"dynamic HuBERT mismatch at {length}: max error {probe_error}"
                )
            print(f"  dynamic length {length}: shape={produced.shape}, max_error={probe_error:.6g}")


if __name__ == "__main__":
    main()
