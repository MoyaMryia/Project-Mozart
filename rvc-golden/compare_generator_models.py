#!/usr/bin/env python3
"""对比两个 ONNX Generator（如原 de_narrator.onnx 与新导出的 generator_dynamic.onnx）。

喂给两个模型完全相同的输入（优先使用 golden 捕获张量，否则按模型各自的
输入规格用同一随机种子合成），逐项比较输出：形状、有限性、max/mean 绝对
误差、RMS、余弦相似度、样本数，并给出 PASS / DEGRADED / FAIL 结论。
可选 --probe-lengths 对声称动态帧数的导出做多长度推理探测（AGENTS.md：
未实测过多长度不得宣称动态轴）。

用法（在板上，模型文件齐备时）：
  python compare_generator_models.py \
      --baseline  rvc-backend/models/de_narrator/de_narrator.onnx \
      --candidate rvc-backend/models/de_narrator/generator_dynamic.onnx \
      --probe-lengths 50,100,200 \
      --output-dir rvc-golden/output

仅用捕获张量时（--tensors-dir 指向 rvc-golden/tensors，含 generator_00_*.npy）：
输入以捕获值为准，两个模型只取各自支持的输入名。

环境自检（无需真模型，构建两个小 ONNX 验证脚本逻辑本身）：
  python compare_generator_models.py --self-test

退出码：0=PASS，1=FAIL，2=DEGRADED。
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parent

# 合成输入的取值范围，对齐 tools/export_generator_onnx.py 的 make_inputs()
FRAMES_DEFAULT = 200
UPP_DEFAULT = 256
FEAT_DIM_DEFAULT = 768
INTER_CHANNELS_DEFAULT = 192
SOURCE_DIM_DEFAULT = 8


def ort_dtype_to_numpy(ort_type: str):
    return {
        "tensor(float)": np.float32,
        "tensor(double)": np.float32,
        "tensor(int64)": np.int64,
        "tensor(int32)": np.int32,
    }.get(ort_type)


def session_specs(session):
    inputs = {
        i.name: {"dtype": ort_dtype_to_numpy(i.type), "shape": list(i.shape)}
        for i in session.get_inputs()
    }
    output_name = session.get_outputs()[0].name
    return inputs, output_name


def resolve_dim(dim, frames: int, upp: int, name: str) -> int:
    if isinstance(dim, int) and dim > 0:
        return dim
    if name in ("feats", "pitch", "pitchf", "latent_noise"):
        return frames
    if name in ("source_noise", "audio"):
        return frames * upp
    return 1


def make_feed(name: str, spec, frames: int, upp: int, rng: np.random.Generator,
              captured):
    """按单个输入的规格生成确定性输入；captured 优先。"""
    if captured is not None:
        return captured
    dtype = spec["dtype"] or np.float32
    shape = [resolve_dim(d, frames, upp, name) for d in spec["shape"]]
    if name == "feats":
        value = rng.standard_normal(shape)
    elif name == "p_len":
        value = np.full(shape, float(shape[0] and frames), dtype=np.float32) \
            if shape else np.array([frames], dtype=np.float32)
    elif name == "pitch":
        value = rng.integers(1, 255, shape).astype(np.float32)
    elif name == "pitchf":
        value = (rng.random(shape) * 150.0 + 70.0).astype(np.float32)
    elif name == "sid":
        value = np.zeros(shape, dtype=np.int64)
    elif name == "latent_noise":
        value = rng.standard_normal(shape)
    elif name == "source_phase":
        value = np.zeros(shape, dtype=np.float32)
    elif name == "source_noise":
        value = rng.standard_normal(shape)
    else:
        value = rng.standard_normal(shape)
    return value.astype(dtype, copy=False)


def load_captured(tensors_dir: Path, prefix: str = "generator_00"):
    """读取 rvc-golden/run_reference.py 捕获的张量（*_<name>.npy）。"""
    if tensors_dir is None:
        return None
    feeds = {}
    for path in sorted(tensors_dir.glob(f"{prefix}_*.npy")):
        name = path.stem[len(prefix) + 1:]
        feeds[name] = np.load(path)
    if not feeds:
        raise FileNotFoundError(f"no captured tensors like {prefix}_*.npy in {tensors_dir}")
    print(f"loaded {len(feeds)} captured tensors from {tensors_dir}")
    return feeds


def run_model(session, output_name, feeds):
    return session.run([output_name], feeds)[0]


def audio_metrics(a: np.ndarray, b: np.ndarray):
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    finite = np.isfinite(a).all() and np.isfinite(b).all()
    if not finite or a.size != b.size or a.size == 0:
        return {"finite": finite, "same_length": a.size == b.size,
                "max_abs": float("inf"), "mean_abs": float("inf"),
                "cosine": -1.0, "rms_a": 0.0, "rms_b": 0.0, "samples": int(a.size)}
    diff = a - b
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    cosine = float(np.dot(a, b) / norm) if norm > 0 else float(a.ravel().tobytes() == b.ravel().tobytes())
    return {
        "finite": True,
        "same_length": True,
        "max_abs": float(np.max(np.abs(diff))),
        "mean_abs": float(np.mean(np.abs(diff))),
        "cosine": cosine,
        "rms_a": float(np.sqrt(np.mean(a * a))),
        "rms_b": float(np.sqrt(np.mean(b * b))),
        "samples": int(a.size),
    }


def verdict(metrics) -> tuple[str, list[str]]:
    notes = []
    if not metrics["finite"]:
        return "FAIL", ["output contains NaN/Inf"]
    if not metrics["same_length"]:
        return "FAIL", ["sample count differs — output duration mismatch"]
    if metrics["cosine"] < 0.999 or metrics["max_abs"] > 1e-2:
        return "FAIL", ["outputs diverge beyond tolerance"]
    if metrics["cosine"] < 0.99999 or metrics["max_abs"] > 1e-4:
        return "DEGRADED", ["outputs are close but not tight — inspect before replacing"]
    return "PASS", []


def probe_lengths(session, specs, output_name, lengths, upp, rng_seed):
    """对含符号维的模型做逐长度推理探测，返回 {length: ok}。"""
    symbolic = any(not isinstance(d, int) or d <= 0
                   for spec in specs.values() for d in spec["shape"])
    results = {}
    for length in lengths:
        rng = np.random.default_rng(rng_seed)
        feeds = {name: make_feed(name, spec, length, upp, rng, None)
                 for name, spec in specs.items()}
        try:
            out = run_model(session, output_name, feeds)
            ok = np.isfinite(out).all()
        except Exception as exc:  # noqa: BLE001 — 探测只关心成败
            print(f"    length {length}: FAILED ({str(exc).splitlines()[0][:100]})")
            results[length] = False
            continue
        print(f"    length {length}: OK ({out.size} samples)")
        results[length] = bool(ok)
    if not symbolic:
        print("    model has fully static shapes; length probing not applicable")
    return results


def compare(args) -> int:
    baseline_path = Path(args.baseline)
    candidate_path = Path(args.candidate)
    for p in (baseline_path, candidate_path):
        if not p.exists():
            print(f"model not found: {p}")
            return 1
    print(f"baseline : {baseline_path}")
    print(f"candidate: {candidate_path}")

    so = ort.SessionOptions()
    so.log_severity_level = 3
    base_sess = ort.InferenceSession(str(baseline_path), so, providers=["CPUExecutionProvider"])
    cand_sess = ort.InferenceSession(str(candidate_path), so, providers=["CPUExecutionProvider"])
    base_inputs, base_out = session_specs(base_sess)
    cand_inputs, cand_out = session_specs(cand_sess)

    captured = load_captured(Path(args.tensors_dir)) if args.tensors_dir else None

    print("\ninput contracts:")
    for label, specs in (("baseline", base_inputs), ("candidate", cand_inputs)):
        pretty = ", ".join(f"{n}{s['shape']}:{(s['dtype'] or '?').__name__}"
                           for n, s in specs.items())
        print(f"  {label:9s} {pretty}")
    only_base = sorted(set(base_inputs) - set(cand_inputs))
    only_cand = sorted(set(cand_inputs) - set(base_inputs))
    if only_base or only_cand:
        print(f"  NOTE input-name differences: baseline-only={only_base} "
              f"candidate-only={only_cand} (each model fed with its own subset)")

    lengths = args.probe_lengths or []
    if lengths:
        print("\ndynamic length probing (candidate):")
        probe_lengths(cand_sess, cand_inputs, cand_out, lengths, args.upp, args.seed)
        print("dynamic length probing (baseline):")
        probe_lengths(base_sess, base_inputs, base_out, lengths, args.upp, args.seed)

    frames = args.frames
    # 每个模型各自从同一种子重新取样：规格一致 ⇒ 输入逐字节一致
    base_feeds = {name: make_feed(name, spec, frames, args.upp,
                                  np.random.default_rng(args.seed),
                                  captured.get(name) if captured else None)
                  for name, spec in base_inputs.items()}
    cand_feeds = {name: make_feed(name, spec, frames, args.upp,
                                  np.random.default_rng(args.seed),
                                  captured.get(name) if captured else None)
                  for name, spec in cand_inputs.items()}

    base_audio = run_model(base_sess, base_out, base_feeds)
    cand_audio = run_model(cand_sess, cand_out, cand_feeds)

    metrics = audio_metrics(base_audio, cand_audio)
    status, notes = verdict(metrics)

    print(f"\nframes={frames} seed={args.seed}")
    print(f"  samples    : baseline={metrics['samples']} candidate="
          f"{cand_audio.size}")
    print(f"  max |diff| : {metrics['max_abs']:.3e}")
    print(f"  mean |diff|: {metrics['mean_abs']:.3e}")
    print(f"  cosine     : {metrics['cosine']:.8f}")
    print(f"  rms        : baseline={metrics['rms_a']:.5f} candidate={metrics['rms_b']:.5f}")
    for note in notes:
        print(f"  note: {note}")

    if args.output_dir and metrics["finite"]:
        try:
            import soundfile as sf
        except ImportError:
            print("  (soundfile not installed; skipping WAV export)")
        else:
            out_dir = Path(args.output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            sf.write(out_dir / f"generator-compare-{frames}-baseline.wav",
                     base_audio.ravel(), 48000, subtype="FLOAT")
            sf.write(out_dir / f"generator-compare-{frames}-candidate.wav",
                     cand_audio.ravel(), 48000, subtype="FLOAT")
            print(f"  wrote audition WAVs to {out_dir}")

    print(f"\nVERDICT: {status}")
    return {"PASS": 0, "DEGRADED": 2, "FAIL": 1}[status]


def self_test() -> int:
    """用两个小 ONNX 模型验证对比逻辑：同权重→PASS，扰动→FAIL。"""
    import tempfile

    import onnx
    from onnx import helper, TensorProto

    def build_model(path: Path, scale: float, dynamic: bool = False):
        w = np.array([0.5, -0.3, 0.2, 0.9, 0.1, -0.7, 0.4, 0.6],
                     dtype=np.float32).reshape(8, 1)
        node_feat = helper.make_node("MatMul", ["feats", "W"], ["y"], name="mm")
        node_tanh = helper.make_node("Tanh", ["y"], ["audio"], name="tanh")
        t_dim = "frames" if dynamic else 4
        graph = helper.make_graph(
            [node_feat, node_tanh], "gen",
            [
                helper.make_tensor_value_info("feats", TensorProto.FLOAT, [t_dim, 8]),
                helper.make_tensor_value_info("sid", TensorProto.INT64, [1]),
            ],
            [helper.make_tensor_value_info("audio", TensorProto.FLOAT, [t_dim, 1])],
            [helper.make_tensor("W", TensorProto.FLOAT, [8, 1],
                                (w * scale).ravel().tolist())],
        )
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
        model.ir_version = 10  # 兼容较旧的 onnxruntime（IR 14 尚未普及）
        onnx.checker.check_model(model)
        onnx.save(model, str(path))

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        build_model(tmp / "a.onnx", 1.0)
        build_model(tmp / "b.onnx", 1.0)
        build_model(tmp / "c.onnx", 1.5)
        build_model(tmp / "dyn.onnx", 1.0, dynamic=True)

        class Args:
            baseline = str(tmp / "a.onnx")
            candidate = str(tmp / "b.onnx")
            frames = 4
            upp = UPP_DEFAULT
            seed = 1234
            probe_lengths = []
            tensors_dir = None
            output_dir = None

        print("── self-test: identical models must PASS ──")
        if compare(Args) != 0:
            failures += 1
        print("── self-test: perturbed model must FAIL ──")
        Args.candidate = str(tmp / "c.onnx")
        if compare(Args) != 1:
            failures += 1
        print("── self-test: dynamic-axes probe must report every length OK ──")
        Args.baseline = str(tmp / "a.onnx")
        Args.candidate = str(tmp / "dyn.onnx")
        Args.probe_lengths = [2, 4, 9]
        if compare(Args) != 0:
            failures += 1

    if failures:
        print(f"\nSELFTEST FAIL ({failures})")
        return 1
    print("\nSELFTEST PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", help="原始 ONNX Generator 路径")
    parser.add_argument("--candidate", help="待验证的新 ONNX Generator 路径")
    parser.add_argument("--frames", type=int, default=FRAMES_DEFAULT,
                        help=f"合成输入的帧数（默认 {FRAMES_DEFAULT}，即 Generator T=200 契约）")
    parser.add_argument("--upp", type=int, default=UPP_DEFAULT,
                        help="每个帧对应的波形采样数（默认 256）")
    parser.add_argument("--seed", type=int, default=114514)
    parser.add_argument("--tensors-dir", help="golden 捕获张量目录（generator_00_*.npy）")
    parser.add_argument("--probe-lengths", default="",
                        help="逗号分隔的帧数列表，对动态轴导出做推理探测")
    parser.add_argument("--output-dir", help="写出 audition WAV 的目录")
    parser.add_argument("--self-test", action="store_true",
                        help="用合成小模型自检脚本逻辑，无需真模型")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.baseline or not args.candidate:
        parser.error("--baseline and --candidate are required (or use --self-test)")
    return compare(args)


if __name__ == "__main__":
    sys.exit(main())
