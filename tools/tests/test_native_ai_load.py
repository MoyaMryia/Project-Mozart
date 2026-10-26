"""校验真实 AI 负载的统计和线程错误传递。"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "native_ai_load.py"
SPEC = importlib.util.spec_from_file_location("native_ai_load", MODULE_PATH)
load = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = load
SPEC.loader.exec_module(load)


class ModelStatisticsTest(unittest.TestCase):
    def test_empty_statistics(self):
        result = load.ModelRun("asr", "zipformer", Path("/model")).report()
        self.assertEqual(result["count"], 0)
        self.assertIsNone(result["latency_s"])

    def test_statistics_preserve_each_inference(self):
        run = load.ModelRun("asr", "zipformer", Path("/model"))
        run.latencies_s.extend([0.1, 0.4, 0.7])
        result = run.report()
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["latencies_s"], [0.1, 0.4, 0.7])
        self.assertAlmostEqual(result["latency_s"]["mean"], 0.4)
        self.assertEqual(result["latency_s"]["median"], 0.4)
        self.assertEqual(result["latency_s"]["min"], 0.1)
        self.assertEqual(result["latency_s"]["max"], 0.7)


class WorkerErrorTest(unittest.TestCase):
    def run_worker(self, loader, stop=None, seconds=1):
        stop = stop or threading.Event()
        run = load.ModelRun("asr", "zipformer", Path("/model"))
        report = {"ready": False, "errors": []}
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready.json"
            load.run_load([run], [loader], seconds, ready, stop, report)
            published = json.loads(ready.read_text()) if ready.exists() else None
        return run, report, published

    def test_load_exception_reaches_coordinator(self):
        def loader():
            raise RuntimeError("模型加载失败")

        run, report, published = self.run_worker(loader)
        self.assertFalse(report["ready"])
        self.assertIsNone(published)
        self.assertEqual(run.report()["count"], 0)
        self.assertEqual(report["errors"][0]["type"], "RuntimeError")
        self.assertIn("模型加载失败", report["errors"][0]["traceback"])

    def test_warmup_exception_prevents_ready(self):
        def infer():
            raise ValueError("模型输出无效")

        _, report, published = self.run_worker(lambda: infer)
        self.assertIsNone(published)
        self.assertEqual(report["errors"][0]["reason"], "模型输出无效")

    def test_inference_exception_preserves_success_count(self):
        calls = 0

        def infer():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise ArithmeticError("推理失败")
            return {"text": "有效输出"}

        run, report, published = self.run_worker(lambda: infer)
        self.assertIsNotNone(published)
        self.assertTrue(report["ready"])
        self.assertEqual(run.report()["count"], 1)
        self.assertEqual(report["errors"][0]["type"], "ArithmeticError")
        self.assertEqual(run.last_output["text"], "有效输出")

    def test_stop_drains_current_inference_and_excludes_warmup(self):
        stop = threading.Event()
        draining = threading.Event()
        calls = 0

        class ObservedExecutor(load.ThreadPoolExecutor):
            def __exit__(self, *args):
                draining.set()
                return super().__exit__(*args)

        def infer():
            nonlocal calls
            calls += 1
            if calls == 2:
                stop.set()
                if not draining.wait(timeout=5):
                    raise RuntimeError("协调线程没有开始收尾")
            return {"samples": calls}

        with mock.patch.object(load, "ThreadPoolExecutor", ObservedExecutor):
            with mock.patch.object(load.time, "monotonic", side_effect=lambda: 20.0 if draining.is_set() else 10.0):
                run, report, published = self.run_worker(lambda: infer, stop)
        self.assertEqual(published["state"], "ready")
        self.assertEqual(published["models"][0]["count"], 0)
        self.assertEqual(run.report()["count"], 1)
        self.assertEqual(run.warmup_output, {"samples": 1})
        self.assertEqual(run.last_output, {"samples": 2})
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["elapsed_s"], 0.0)
        self.assertEqual(report["elapsed_with_drain_s"], 10.0)
        self.assertEqual(run.latencies_s, [10.0])

    def test_ready_write_failure_stops_waiting_worker(self):
        with mock.patch.object(load, "atomic_json", side_effect=PermissionError("无法发布 ready")):
            run, report, published = self.run_worker(lambda: lambda: {"text": "预热"})
        self.assertIsNone(published)
        self.assertFalse(report["ready"])
        self.assertEqual(run.report()["count"], 0)
        self.assertEqual(report["errors"][0]["type"], "PermissionError")

    def test_worker_failure_releases_initialized_peer(self):
        def loader():
            raise RuntimeError("第二个模型失败")

        runs = [load.ModelRun(name, "zipformer", Path("/model")) for name in ["asr", "second_asr"]]
        report = {"ready": False, "errors": []}
        with tempfile.TemporaryDirectory() as directory:
            load.run_load(runs, [lambda: lambda: {"text": "预热"}, loader], 1,
                          Path(directory) / "ready.json", threading.Event(), report)
        self.assertFalse(report["ready"])
        self.assertEqual(report["errors"][0]["worker"], "second_asr")
        self.assertEqual(sum(len(run.latencies_s) for run in runs), 0)


class PathSafetyTest(unittest.TestCase):
    def configuration(self, root, asr_kind="zipformer", second_kind="pocket-tts"):
        models = [(asr_kind, root / "asr"), (second_kind, root / "second")]
        files = {"zipformer": load.ZIPFORMER_FILES, "sense-voice": load.SENSEVOICE_FILES,
                 "pocket-tts": load.POCKET_FILES}
        for kind, directory in models:
            directory.mkdir()
            for name in files[kind].values():
                (directory / name).write_bytes(f"original {kind} {name}".encode())
        reference = root / "second/test_wavs/bria.wav"
        reference.parent.mkdir()
        reference.write_bytes(b"original default reference")
        (root / "input.wav").write_bytes(b"original input")
        (root / "result.json").write_bytes(b"original result")
        (root / "ready.json").write_bytes(b"original ready")
        second_option = "--tts-model" if second_kind == "pocket-tts" else "--second-asr-model"
        args = ["--asr-model", str(models[0][1]), "--asr-kind", asr_kind,
                second_option, str(models[1][1]), "--wav", str(root / "input.wav"),
                "--output", str(root / "result.json"), "--ready-file", str(root / "ready.json")]
        if second_kind != "pocket-tts":
            args.extend(["--second-asr-kind", second_kind])
        return args, models

    def set_option(self, args, option, path):
        args[args.index(option) + 1] = str(path)

    def assert_rejected_without_changes(self, root, args, message):
        original_paths = set(root.rglob("*"))
        original_bytes = {path: path.read_bytes() for path in original_paths if path.is_file()}
        stderr = io.StringIO()
        with mock.patch.object(load, "ModelRun") as model_run:
            with mock.patch.object(load, "run_load") as run_load:
                with mock.patch.object(load, "atomic_json", wraps=load.atomic_json) as write:
                    with contextlib.redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as caught:
                            load.main(args)
                    self.assertEqual(caught.exception.code, 2)
                    self.assertIn(message, stderr.getvalue())
                    model_run.assert_not_called()
                    run_load.assert_not_called()
                    write.assert_not_called()
        self.assertEqual(set(root.rglob("*")), original_paths)
        for path, content in original_bytes.items():
            self.assertEqual(path.read_bytes(), content, str(path))

    def test_audio_collisions_preserve_input_and_existing_reports(self):
        for option in ["--output", "--ready-file"]:
            for source in ["wav", "default-reference", "explicit-reference", "linked-default-reference"]:
                for alias in ["direct", "relative", "dotdot", "symlink"]:
                    with self.subTest(option=option, source=source, alias=alias):
                        with tempfile.TemporaryDirectory() as directory:
                            root = Path(directory)
                            args, _ = self.configuration(root)
                            target = root / "input.wav"
                            if source == "default-reference":
                                target = root / "second/test_wavs/bria.wav"
                            elif source in ["explicit-reference", "linked-default-reference"]:
                                target = root / "reference.wav"
                                target.write_bytes(b"original external reference")
                                if source == "explicit-reference":
                                    args.extend(["--tts-reference", str(target)])
                                else:
                                    default = root / "second/test_wavs/bria.wav"
                                    default.unlink()
                                    default.symlink_to(target)
                            destination = target
                            if alias == "relative":
                                destination = Path(os.path.relpath(target))
                            elif alias == "dotdot":
                                (root / "alias").mkdir()
                                destination = root / "alias/.." / target.relative_to(root)
                            elif alias == "symlink":
                                destination = root / "alias"
                                destination.symlink_to(target)
                            self.set_option(args, option, destination)
                            self.assert_rejected_without_changes(root, args, option)

    def test_selected_assets_include_symlinks_outside_model_directory(self):
        files = {"zipformer": load.ZIPFORMER_FILES, "sense-voice": load.SENSEVOICE_FILES,
                 "pocket-tts": load.POCKET_FILES}
        for asr_kind, second_kind in [("zipformer", "pocket-tts"), ("sense-voice", "zipformer"),
                                      ("zipformer", "sense-voice")]:
            for index, kind in enumerate([asr_kind, second_kind]):
                for name in files[kind].values():
                    for option in ["--output", "--ready-file"]:
                        for external in [False, True]:
                            with self.subTest(asr=asr_kind, second=second_kind, index=index,
                                              asset=name, option=option, external=external):
                                with tempfile.TemporaryDirectory() as directory:
                                    root = Path(directory)
                                    args, models = self.configuration(root, asr_kind, second_kind)
                                    asset = models[index][1] / name
                                    target = asset
                                    if external:
                                        target = root / "external-asset"
                                        asset.rename(target)
                                        asset.symlink_to(target)
                                    self.set_option(args, option, target)
                                    self.assert_rejected_without_changes(root, args, "模型资产路径重合")

    def test_model_directory_tree_includes_directory_aliases(self):
        for option in ["--output", "--ready-file"]:
            for second_kind in ["pocket-tts", "sense-voice"]:
                for index in [0, 1]:
                    for alias in ["root", "nested", "output-directory-link", "model-directory-link"]:
                        with self.subTest(option=option, second=second_kind, index=index, alias=alias):
                            with tempfile.TemporaryDirectory() as directory:
                                root = Path(directory)
                                args, models = self.configuration(root, second_kind=second_kind)
                                model_dir = models[index][1]
                                destination = model_dir if alias == "root" else model_dir / "new/result.json"
                                if alias in ["output-directory-link", "model-directory-link"]:
                                    link = root / "model-alias"
                                    link.symlink_to(model_dir, target_is_directory=True)
                                    if alias == "output-directory-link":
                                        destination = link / "new/result.json"
                                    else:
                                        model_option = "--asr-model" if index == 0 else (
                                            "--tts-model" if second_kind == "pocket-tts" else "--second-asr-model")
                                        self.set_option(args, model_option, link)
                                self.set_option(args, option, destination)
                                self.assert_rejected_without_changes(root, args, "模型目录树之外")

    def test_report_paths_cannot_alias_each_other(self):
        for alias in ["direct", "relative", "dotdot", "symlink"]:
            with self.subTest(alias=alias), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                args, _ = self.configuration(root)
                destination = root / "result.json"
                if alias == "relative":
                    destination = Path(os.path.relpath(destination))
                elif alias == "dotdot":
                    (root / "alias").mkdir()
                    destination = root / "alias/../result.json"
                elif alias == "symlink":
                    destination = root / "alias"
                    destination.symlink_to(root / "result.json")
                self.set_option(args, "--ready-file", destination)
                self.assert_rejected_without_changes(root, args, "必须使用不同路径")

    def test_path_resolution_errors_stop_at_parser(self):
        for option in ["--wav", "--asr-model", "--tts-reference", "--output", "--ready-file",
                       "asset", "default-reference"]:
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                args, models = self.configuration(root)
                loop = root / "loop"
                loop.symlink_to(loop)
                if option in ["asset", "default-reference"]:
                    target = (models[0][1] / next(iter(load.ZIPFORMER_FILES.values())) if option == "asset"
                              else root / "second/test_wavs/bria.wav")
                    target.unlink()
                    target.symlink_to(loop)
                elif option == "--tts-reference":
                    args.extend([option, str(loop)])
                else:
                    self.set_option(args, option, loop)
                self.assert_rejected_without_changes(root, args, "路径解析失败")

    def test_unused_reference_and_missing_assets_do_not_block_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = ["--asr-model", str(root / "missing-asr"),
                    "--second-asr-model", str(root / "missing-second-asr"),
                    "--tts-reference", str(root / "result.json"), "--wav", str(root / "input.wav"),
                    "--output", str(root / "result.json"), "--ready-file", str(root / "ready.json")]
            parsed = load.arguments(args)
            self.assertEqual(parsed.output, root / "result.json")
            self.assertEqual(list(root.iterdir()), [])


class CoordinatorTest(unittest.TestCase):
    def arguments(self, directory):
        root = Path(directory)
        return [
            "--asr-model", str(root / "zipformer"), "--tts-model", str(root / "pocket"),
            "--wav", str(root / "input.wav"), "--seconds", "1",
            "--output", str(root / "result.json"), "--ready-file", str(root / "ready.json"),
        ]

    def test_worker_exception_returns_exit_one_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(load, "load_model", side_effect=RuntimeError("模型不可用")):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = load.main(self.arguments(directory))
            result = json.loads((Path(directory) / "result.json").read_text())
            self.assertEqual(code, 1)
            self.assertFalse(result["passed"])
            self.assertEqual(result["errors"][0]["reason"], "模型不可用")
            self.assertFalse((Path(directory) / "ready.json").exists())

    def test_sigterm_stops_workers_and_saves_completed_inferences(self):
        barrier = threading.Barrier(2)

        def loader(run, _wav, _text, _reference):
            calls = 0

            def infer():
                nonlocal calls
                calls += 1
                if calls == 2:
                    barrier.wait(timeout=5)
                    if run.name == "asr":
                        os.kill(os.getpid(), signal.SIGTERM)
                return {"text": "有效输出"}

            return infer

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(load, "load_model", side_effect=loader):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = load.main(self.arguments(directory))
            result = json.loads((Path(directory) / "result.json").read_text())
            self.assertEqual(code, 0)
            self.assertEqual(result["signal"], "SIGTERM")
            self.assertTrue(all(model["count"] >= 1 for model in result["models"]))
            self.assertFalse((Path(directory) / "ready.json").exists())

    def test_sigterm_during_ready_publish_remains_pending_until_start(self):
        original = load.atomic_json
        observed_pending = []

        def write_with_signal(path, value):
            if path.name == "ready.json":
                os.kill(os.getpid(), signal.SIGTERM)
                observed_pending.append(signal.SIGTERM in signal.sigpending())
            original(path, value)

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(load, "load_model", return_value=lambda: {"text": "有效输出"}):
                with mock.patch.object(load, "atomic_json", side_effect=write_with_signal):
                    with contextlib.redirect_stdout(io.StringIO()):
                        code = load.main(self.arguments(directory))
            result = json.loads((Path(directory) / "result.json").read_text())
            self.assertEqual(observed_pending, [True])
            self.assertTrue(result["ready"])
            self.assertEqual(result["signal"], "SIGTERM")
            self.assertEqual(code, 0 if result["passed"] else 1)
            self.assertFalse((Path(directory) / "ready.json").exists())

    def test_sigterm_during_report_does_not_interrupt_final_write(self):
        original = load.atomic_json

        def write_with_signal(path, value):
            if path.name == "result.json":
                os.kill(os.getpid(), signal.SIGTERM)
            original(path, value)

        previous = signal.getsignal(signal.SIGTERM)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(load, "load_model", side_effect=RuntimeError("模型不可用")):
                with mock.patch.object(load, "atomic_json", side_effect=write_with_signal):
                    with contextlib.redirect_stdout(io.StringIO()):
                        code = load.main(self.arguments(directory))
            result = json.loads((Path(directory) / "result.json").read_text())
            self.assertEqual(code, 1)
            self.assertEqual(result["signal"], "SIGTERM")
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_ready_contains_complete_json_at_atomic_replace(self):
        original_replace = os.replace
        replacements = []
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "ready.json"

            def replace(source, target):
                self.assertFalse(destination.exists())
                self.assertEqual(Path(target), destination)
                replacements.append(json.loads(Path(source).read_text()))
                original_replace(source, target)

            with mock.patch.object(load.os, "replace", side_effect=replace):
                load.atomic_json(destination, {"state": "ready", "models": ["zipformer", "pocket-tts"]})
            self.assertEqual(replacements[0]["state"], "ready")
            self.assertEqual(json.loads(destination.read_text()), replacements[0])
            self.assertEqual(list(Path(directory).iterdir()), [destination])


if __name__ == "__main__":
    unittest.main()
