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
    args = parser.parse_args()

    model = HubertModel.from_pretrained(
        Path(args.hubert_path), local_files_only=True, torch_dtype=torch.float32
    ).eval()
    wrapper = RvcV2Hubert(model).eval()
    dummy = torch.randn(1, 32000)

    with torch.inference_mode():
        reference = wrapper(dummy).numpy()

    torch.onnx.export(
        wrapper,
        dummy,
        args.output,
        input_names=["audio"],
        output_names=["features"],
        opset_version=args.opset,
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


if __name__ == "__main__":
    main()
