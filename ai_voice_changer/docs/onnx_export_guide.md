# ONNX Export Guide — Client-Side (For Speaker Model Authors)

This document is for the **training side** (typically a workstation with a
powerful GPU). The Jetson post-process backend only consumes ONNX — it has no
PyTorch. Therefore the speaker ONNX must be produced on the training machine
before upload.

## 1. What to upload

```
models/
└── {speaker_id}/
    ├── {speaker_id}.onnx     # REQUIRED, the exported RVC generator
    └── config.json           # REQUIRED, the model config used at export time
```

The optional `.index` retrieval file is **not used in Phase 2** (the backend
runs with `index_rate = 0`). It can be uploaded and ignored.

## 2. Pretrained assets installed on Jetson (NOT uploaded)

These are fixed pretrained models the backend expects **already** present under
`assets/`. They are downloaded once during setup, not per speaker.

| File | Source |
|---|---|
| `assets/hubert_base.onnx` | Export from `hubert_base.pt` (see §4 below) |
| `assets/rmvpe.onnx` | The official `rmvpe.onnx`; download from the RVC HuggingFace repo |

Client training machines that already use PyTorch to train RVC can reuse the
same export path for HuBERT/RMVPE.

## 3. Exporting the speaker generator from `.pth`

Use the official RVC training repo's `infer.lib.infer_pack.models` to
instantiate the model, load the trained state dict, and `torch.onnx.export`.

```python
# export_speaker_onnx.py (run on the training machine, NOT on Jetson)
import torch
import json
from pathlib import Path

# These imports come from the RVC training repo; clone it locally.
from infer.lib.infer_pack.models import (
    SynthesizerTrnMs256NSFsid,
    SynthesizerTrnMs256NSFsid_nono,
    SynthesizerTrnMs768NSFsid,
    SynthesizerTrnMs768NSFsid_nono,
)

def export(speaker_id: str,
           pth_path: str,
           config_path: str,
           out_dir: str = "out") -> None:
    with open(config_path, "r") as f:
        cfg = json.load(f)

    f0 = cfg.get("f0", 1)
    version = cfg.get("version", "v2")  # v2 default for 48k
    sample_rate = cfg["data"]["sampling_rate"]
    emb_channels = 768 if version == "v2" else 256

    # Pick the architecture based on f0 flag and version.
    if f0 == 1:
        net_cls = SynthesizerTrnMs768NSFsid if version == "v2" else SynthesizerTrnMs256NSFsid
    else:
        net_cls = SynthesizerTrnMs768NSFsid_nono if version == "v2" else SynthesizerTrnMs256NSFsid_nono

    # Inspect config or use the repo's convenience builder; below stands in
    # for the actual `__init__` signature. The exact kwargs depend on the RVC
    # repo's models.py.
    net_g = net_cls(
        256,          # inter_channels
        emb_channels,  # emb_channels
        192,          # gin_channels
        768,          # hp hidden
        16,           # n_flow_layer
        pre_embedded=True,
    )

    ckpt = torch.load(pth_path, map_location="cpu")
    state = ckpt.get("model", ckpt)     # RVC stores under "weight" sometimes
    if "weight" in ckpt:
        state = ckpt["weight"]
    net_g.load_state_dict(state, strict=False)
    net_g.eval()

    # Stand-in example input. Replace lengths with the actual frame hop your
    # exporter expects. The ONNX I/O names MUST align with the backend's
    # generator.cpp convention:
    #   phone (f32), phone_lengths (i64), pitch (f32/cast int), pitchf (f32),
    #   sid (i64 or f32). The backend wraps both float/int forms.
    phone = torch.randn(1, 100, emb_channels)
    phone_lengths = torch.LongTensor([100])
    sid = torch.LongTensor([0])
    if f0 == 1:
        pitch = torch.zeros(1, 100, dtype=torch.long)
        pitchf = torch.zeros(1, 100, dtype=torch.float32)
        inputs = (phone, phone_lengths, pitch, pitchf, sid)
        input_names = ["phone", "phone_lengths", "pitch", "pitchf", "sid"]
    else:
        inputs = (phone, phone_lengths, sid)
        input_names = ["phone", "phone_lengths", "sid"]

    out = Path(out_dir) / speaker_id
    out.mkdir(parents=True, exist_ok=True)
    onnx_path = out / f"{speaker_id}.onnx"
    torch.onnx.export(
        net_g, inputs, str(onnx_path),
        input_names=input_names,
        output_names=["audio"],
        dynamic_axes={
            "phone": {0: "T"},
            "pitch": {0: "T"} if f0 == 1 else {},
            "pitchf": {0: "T"} if f0 == 1 else {},
            "audio": {2: "T_audio"},
        },
        opset_version=14,
        do_constant_folding=True,
    )
    # Copy the config alongside so the backend can read sample_rate etc.
    import shutil
    shutil.copy(config_path, out / "config.json")
    print(f"Exported: {onnx_path}")

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--pth", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default="out")
    args = ap.parse_args()
    export(args.id, args.pth, args.config, args.out)
```

> **Note**: the constructor arguments above are illustrative — the exact
> `__init__` signature of your RVC repo may differ. The contract fields that
> matter to the backend are listed in `include/rvc/generator.hpp`; any explicit
> ONNX I/O name mismatch should be reconciled there.

## 4. Exporting HuBERT to ONNX (one-time setup)

The backend ships with `assets/hubert_base.onnx`. To (re)build it from the
`hubert_base.pt` checkpoint used by RVC, run the following on a machine with
`fairseq` + PyTorch installed (typically the training workstation):

```python
export_hubert_onnx.py
import torch
from fairseq import checkpoint_utils

models, _, _ = checkpoint_utils.load_model_ensemble_and_task(["hubert_base.pt"])
model = models[0].eval()

# Single-channel 16k waveform input.
# The default RVC export uses input name "source" and output "hidden".
dummy = torch.randn(1, 16000)  # 1 second, 16k

torch.onnx.export(
    model, dummy, "assets/hubert_base.onnx",
    input_names=["source"],
    output_names=["hidden"],
    dynamic_axes={"source": {1: "T"}, "hidden": {1: "T"}},
    opset_version=14,
)
print("Exported hubert_base.onnx")
```

If your `fairseq` HuBERT does not accept raw `(1, T)` input, wrap it in a thin
module exposing the standard signature. The backend expects exactly one float
input named `source` of shape `[1, T]` and one float output named `hidden`
(typically shape `[1, T', 768]`).

## 5. Downloading RMVPE ONNX

The RVC project publishes a pre-built ONNX:

```bash
wget https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/rmvpe.onnx \
  -O assets/rmvpe.onnx
```

Spec:
- Input: `"audio"` float32 shape `[1, N]` (N = number of 16k samples)
- Output: `"f0"` float32 shape `[N' / hop]`

If the names differ, adjust `src/rvc/rmvpe_f0.cpp` accordingly.

## 6. Uploading a speaker model

```bash
curl -F "model_id=speaker_a" \
     -F "pth_file=@speaker_a.onnx" \
     -F "config_file=@config.json" \
     http://jetson-ip:18080/models/upload

curl -X POST http://jetson-ip:18080/models/speaker_a/activate
```

> The HTTP field name `pth_file` is kept for compatibility; the actual payload
> MUST be the `.onnx` file. The skeleton `/models/upload` endpoint writes to
> `models/{speaker_id}/`; expand the handler to store all parts if you extend
> it.