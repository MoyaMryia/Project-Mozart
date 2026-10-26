"""验收工具的确定性产物、错误注入和当前 C DSP 对拍。"""

import contextlib
import csv
import ctypes
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import wave

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verify_native_usb as verify


ROOT = Path(__file__).resolve().parents[2]
P = verify.PROTOCOL


def write_wav(path, samples, rate=None, channels=None, width=None):
    with wave.open(str(path), "wb") as output:
        output.setframerate(P["RATE"] if rate is None else rate)
        output.setnchannels(P["CHANNELS"] if channels is None else channels)
        output.setsampwidth(P["SAMPLE_BYTES"] if width is None else width)
        output.writeframes(samples.astype("<i2").tobytes())


class ArtifactFixture(unittest.TestCase):
    def setUp(self):
        directory = ROOT / "build" / "run"
        directory.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.status_path = self.directory / "status.json"
        self.library = self.directory / "reference.so"
        self.output = self.directory / "summary.json"
        self.make_fixture()

    def make_fixture(self, negotiated=True, warmup=100, steady=4, tail=7):
        block = P["FAST64_SAMPLES"] if negotiated else P["FAST_SAMPLES"]
        total = warmup + steady
        frames = (total - 1) * block + tail
        index = np.arange(frames, dtype=np.int32)
        left = ((index * 173 + 19) % 20001 - 10000).astype(np.int16)
        self.source = np.column_stack((left, -left))
        self.actual = np.column_stack((left, left))
        write_wav(self.directory / "capture.wav", self.source)
        write_wav(self.directory / "response.wav", self.actual)
        self.rows = []
        rtts = []
        for seq in range(total):
            stamp = 1000000 + seq * 2000
            # 首包超预算用于确认预热只影响稳态筛选。
            rtt = 14000 if seq == 0 and warmup else 100 + seq
            valid = tail if seq == total - 1 else block
            self.rows.extend([
                {"direction": "tx", "seq": seq, "host_us": stamp,
                 "wire_timestamp_us": stamp, "rtt_us": 0, "source_frames": valid},
                {"direction": "rx", "seq": seq, "host_us": stamp + rtt,
                 "wire_timestamp_us": stamp if negotiated else 900000000 + seq,
                 "rtt_us": rtt, "source_frames": valid},
            ])
            rtts.append(rtt)
        # 首包较慢时保持每个方向的本机接收顺序。
        for row in self.rows:
            if row["seq"] > 0:
                row["host_us"] += 20000
                if row["direction"] == "tx" or negotiated:
                    row["wire_timestamp_us"] += 20000
        values = sorted(rtts)
        self.status = {
            "state": "stopped", "reason": "normal_stop", "negotiated": negotiated,
            "block": block, "stream": P["STREAM_FAST64"] if negotiated else P["STREAM_FAST"],
            "tx": total, "rx": total, "played": steady, "warmup": warmup,
            "source_frames": frames, "source_total_frames": frames + 100,
            "padding_frames": block - tail, "output_frames": frames,
            "xrun": {"capture_overrun": 0, "playback_underrun": 0,
                     "capture_suspend": 0, "playback_suspend": 0},
            "rtt_us": {"count": total, "max": max(rtts), "bucket_us": 32,
                       **{f"p{p}": min(max(rtts), (values[(total * p + 99) // 100 - 1] // 32 + 1) * 32 - 1)
                          for p in (50, 99)}},
        }
        self.save()

    def make_hardware(self):
        self.status["source_total_frames"] = 0
        self.status["output_frames"] -= min(self.status["output_frames"], self.status["warmup"] * self.status["block"])
        self.status["alsa_e2e_queue_estimate_us"] = {
            "estimated": True, "physical_measurement": False,
            "count": self.status["played"], "p50": 6750, "p99": 6750, "max": 6750,
            "bucket_us": 32, "budget_us": P["DIGITAL_LATENCY_BUDGET_US"],
            "over_budget_count": 0,
            "max_evidence": {"seq": self.status["warmup"], "capture_frames": 36,
                             "playback_frames": 192, "delta_us": 2000},
        }
        self.save()

    def save(self):
        self.status_path.write_text(json.dumps(self.status))
        with (self.directory / "packets.csv").open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=verify.CSV_FIELDS)
            writer.writeheader()
            writer.writerows(self.rows)

    def run_verify(self, budget=verify.DEFAULT_STEADY_BUDGET_MS):
        # 固定真值替身只隔离 C 边界，产物校验和验收条件均真实执行。
        with mock.patch.object(verify, "reference_pcm", return_value=self.source[:, 0].copy()) as reference:
            summary = verify.verify(self.directory, self.status_path, self.library, budget)
        self.assertEqual(reference.call_count, 1)
        return summary

    def assert_fails(self, fragment):
        summary = self.run_verify()
        self.assertFalse(summary["passed"])
        self.assertTrue(any(fragment in error for error in summary["errors"]), summary["errors"])
        return summary


class AcceptanceTests(ArtifactFixture):
    def test_native64_pass_and_full_warmup_audio(self):
        summary = self.run_verify()
        self.assertTrue(summary["passed"], summary["errors"])
        self.assertEqual(summary["audio"]["compared_samples"], self.source.size)
        self.assertEqual(summary["trace"]["steady"]["count"], self.status["played"])
        self.assertEqual(summary["trace"]["steady"]["max_us"], 203)
        self.assertEqual(summary["trace"]["all"]["max_us"], 14000)
        self.assertEqual([row["seq"] for row in summary["trace"]["all"]["over_budget"]], [0])

    def test_hardware_trace_keeps_warmup_but_playback_excludes_it(self):
        self.make_hardware()
        summary = self.run_verify()
        self.assertTrue(summary["passed"], summary["errors"])
        self.assertEqual(summary["audio"]["compared_samples"], self.source.size)
        self.status["output_frames"] -= 1
        self.save()
        self.assert_fails("status.output_frames")

    def test_hardware_trace_still_requires_every_returned_sample(self):
        self.make_hardware()
        write_wav(self.directory / "response.wav", self.actual[1:])
        self.assert_fails("capture/response WAV 长度不一致")

    def test_hardware_estimate_budget_and_coverage_fail_independently(self):
        for kind in ("missing", "count", "max", "counter", "formula", "scope", "budget"):
            with self.subTest(kind=kind):
                self.make_fixture()
                self.make_hardware()
                metric = self.status["alsa_e2e_queue_estimate_us"]
                if kind == "missing":
                    del self.status["alsa_e2e_queue_estimate_us"]
                elif kind == "count":
                    metric["count"] -= 1
                elif kind == "max":
                    metric["max"] = metric["budget_us"] + 1
                elif kind == "counter":
                    metric["over_budget_count"] = 1
                elif kind == "formula":
                    metric["max_evidence"]["delta_us"] += 1
                elif kind == "scope":
                    metric["max_evidence"]["seq"] = 0
                else:
                    metric["budget_us"] += 1
                self.save()
                self.assert_fails("ALSA")

    def test_legacy_remote_clock_is_not_subtracted(self):
        self.make_fixture(negotiated=False)
        summary = self.run_verify()
        self.assertTrue(summary["passed"], summary["errors"])

    def test_increased_warmup_cannot_hide_a_slow_steady_packet(self):
        for row in self.rows:
            if row["direction"] == "rx" and row["seq"] == self.status["warmup"]:
                row["rtt_us"] = 12000
                row["host_us"] = row["wire_timestamp_us"] + row["rtt_us"]
        self.status["warmup"] += 1
        self.status["played"] -= 1
        self.save()
        self.assert_fails("status.warmup")

    def test_failed_and_nonterminal_status_fail(self):
        for state in ("failed", "ready", "warming"):
            with self.subTest(state=state):
                self.status["state"] = state
                self.status["reason"] = "injected failure"
                self.save()
                summary = self.assert_fails("status.state")
                self.assertIsNotNone(summary["audio"]["max_error_lsb"])
                self.assertEqual(summary["status"]["reason"], "injected failure")

    def test_badcount_and_xrun_fail(self):
        for extra in ({"badcount": 1}, {"usb": {"bad_count": 1}},
                      {"xrun": {"capture_overrun": 0, "playback_underrun": 1,
                                "capture_suspend": 0, "playback_suspend": 0}}):
            with self.subTest(extra=extra):
                self.make_fixture()
                self.status.update(extra)
                self.save()
                summary = self.assert_fails("badcount")
                self.assertEqual(summary["faults"][0]["value"], 1)

    def test_sequence_gap_duplicate_reorder_and_missing_rx(self):
        for kind in ("gap", "duplicate", "reorder", "missing"):
            with self.subTest(kind=kind):
                self.make_fixture()
                if kind == "gap":
                    self.rows[5]["seq"] += 1
                elif kind == "duplicate":
                    self.rows[5]["seq"] -= 1
                elif kind == "reorder":
                    self.rows[5], self.rows[7] = self.rows[7], self.rows[5]
                else:
                    self.rows.pop(5)
                self.save()
                self.assert_fails("序号不连续")

    def test_rx_frames_must_correspond_to_tx(self):
        self.rows[-1]["source_frames"] -= 1
        self.save()
        self.assert_fails("TX/RX 对应")

    def test_host_rtt_and_native_wire_echo(self):
        for field in ("host_us", "rtt_us", "wire_timestamp_us"):
            with self.subTest(field=field):
                self.make_fixture()
                self.rows[-1][field] += 1
                self.save()
                self.assert_fails("wire echo" if field == "wire_timestamp_us" else "本机时戳 RTT")

    def test_tx_stamp_and_nonzero_rtt_fail(self):
        for field in ("wire_timestamp_us", "rtt_us"):
            with self.subTest(field=field):
                self.make_fixture()
                self.rows[0][field] += 1
                self.save()
                self.assert_fails("TX 本机时间")

    def test_negative_elapsed_and_clock_reversal_fail(self):
        self.rows[-1]["host_us"] = self.rows[-3]["host_us"] - 1
        self.save()
        summary = self.assert_fails("本机时戳 RTT")
        self.assertTrue(any("本机时间倒退" in error for error in summary["errors"]))

    def test_exact_budget_and_all_over_budget_packets(self):
        for seq in (100, 101):
            row = self.rows[seq * 2 + 1]
            row["rtt_us"] = 10000 + seq - 100
            row["host_us"] = self.rows[seq * 2]["host_us"] + row["rtt_us"]
        # 重新安排后两包时戳，避免 RTT 用例混入时间倒退。
        for row in self.rows[204:]:
            row["host_us"] += 20000
            row["wire_timestamp_us"] += 20000
        self.status["rtt_us"].update(p99=10015)
        self.save()
        summary = self.assert_fails("稳态 RTT 超预算")
        self.assertEqual([row["seq"] for row in summary["trace"]["steady"]["over_budget"]], [101])
        relaxed = self.run_verify(11)
        self.assertTrue(relaxed["passed"], relaxed["errors"])
        self.assertEqual(relaxed["steady_budget_ms"], 11)

    def test_multiple_maxima_are_preserved(self):
        rows = [{"seq": seq, "rtt_us": value} for seq, value in enumerate([4, 8, 8, 1])]
        stats = verify.statistics(rows, 5)
        self.assertEqual([row["seq"] for row in stats["max_packets"]], [1, 2])
        self.assertEqual([row["seq"] for row in stats["over_budget"]], [1, 2])
        self.assertEqual(stats["mean_us"], 5.25)
        self.assertEqual(stats["p50_us"], 4)
        self.assertEqual(stats["p95_us"], 8)
        self.assertEqual(stats["p99_us"], 8)

    def test_no_steady_samples_fails(self):
        self.make_fixture(steady=0)
        self.assert_fails("稳态 RX 样本为空")

    def test_status_counts_and_all_rx_histogram(self):
        for field in ("tx", "rx", "played", "source_frames", "output_frames", "padding_frames"):
            with self.subTest(field=field):
                self.make_fixture()
                self.status[field] += 1
                self.save()
                self.assert_fails(f"status.{field}")
        for field in ("count", "max", "p50", "p99"):
            with self.subTest(histogram=field):
                self.make_fixture()
                self.status["rtt_us"][field] += 1000
                self.save()
                self.assert_fails("status.rtt_us")

    def test_histogram_bucket_contract_matches_native_host(self):
        source = (ROOT / "usb-audio/native-host/runtime.hpp").read_text()
        match = re.search(r"std::array<uint64_t,\s*(\d+)>\s+bins", source)
        self.assertIsNotNone(match)
        self.assertEqual(verify.STATUS_HISTOGRAM_BINS, int(match.group(1)))

    def test_histogram_rejects_within_bucket_error(self):
        exact = verify.statistics([row for row in self.rows if row["direction"] == "rx"], 10000)["p50_us"]
        self.assertNotEqual(self.status["rtt_us"]["p50"], exact)
        self.status["rtt_us"]["p50"] = exact
        self.save()
        self.assert_fails("status.rtt_us.p50")

    def test_histogram_saturated_warmup_still_uses_raw_steady_values(self):
        rtts = []
        for seq in range(self.status["tx"]):
            sent, received = self.rows[seq * 2:seq * 2 + 2]
            stamp = 1000000 + seq * 1000000
            rtt = 200000 + seq * 1000 if seq < self.status["warmup"] else 100 + seq
            sent.update(host_us=stamp, wire_timestamp_us=stamp)
            received.update(host_us=stamp + rtt, wire_timestamp_us=stamp, rtt_us=rtt)
            rtts.append(rtt)
        self.status["rtt_us"].update(max=max(rtts), p50=max(rtts), p99=max(rtts))
        self.save()
        summary = self.run_verify()
        self.assertTrue(summary["passed"], summary["errors"])
        self.assertEqual(summary["trace"]["steady"]["max_us"], 203)
        self.assertNotEqual(summary["trace"]["all"]["p50_us"], self.status["rtt_us"]["p50"])

    def test_frame_range_and_nonfinal_short_block_fail(self):
        for frames in (0, P["FAST64_SAMPLES"] + 1, 7):
            with self.subTest(frames=frames):
                self.make_fixture()
                self.rows[2]["source_frames"] = frames
                self.save()
                self.assert_fails("source_frames" if frames != 7 else "非末包被截短")

    def test_audio_error_one_lsb_pass_two_lsb_fail_both_channels(self):
        for channel in range(P["CHANNELS"]):
            for delta in (1, 2):
                with self.subTest(channel=channel, delta=delta):
                    actual = self.actual.copy()
                    actual[-1, channel] += delta
                    write_wav(self.directory / "response.wav", actual)
                    summary = self.run_verify()
                    self.assertEqual(summary["passed"], delta <= verify.MAX_ERROR_LSB)
                    self.assertEqual(summary["audio"]["max_error_lsb"], delta)
                    self.assertEqual(summary["audio"]["bad_sample_count"], int(delta > verify.MAX_ERROR_LSB))

    def test_warmup_audio_is_compared(self):
        actual = self.actual.copy()
        actual[0, 0] += 2
        write_wav(self.directory / "response.wav", actual)
        self.assert_fails("C DSP 逐样本误差")

    def test_shortened_response_and_missing_source_padding_fail(self):
        write_wav(self.directory / "response.wav", self.actual[:-1])
        self.assert_fails("WAV 长度不一致")
        self.make_fixture()
        padded = np.pad(self.actual, ((0, self.status["padding_frames"]), (0, 0)))
        write_wav(self.directory / "response.wav", padded)
        self.assert_fails("WAV 长度不一致")

    def test_wav_format_and_truncation_fail(self):
        for kwargs in ({"rate": 16000}, {"channels": 1}, {"width": 1}):
            with self.subTest(kwargs=kwargs):
                self.make_fixture()
                write_wav(self.directory / "capture.wav", self.source, **kwargs)
                summary = verify.verify(self.directory, self.status_path, self.library)
                self.assertFalse(summary["passed"])
                self.assertTrue(any("48 kHz" in error for error in summary["errors"]))
        self.make_fixture()
        path = self.directory / "capture.wav"
        path.write_bytes(path.read_bytes()[:-2])
        summary = verify.verify(self.directory, self.status_path, self.library)
        self.assertTrue(any("PCM 数据截短" in error for error in summary["errors"]))

    def test_schema_and_numbers_fail(self):
        for text in ("seq,direction\n0,tx\n", ",".join(verify.CSV_FIELDS) + "\ntx,0,1,1,-1,64\n",
                     ",".join(verify.CSV_FIELDS) + "\nbad,0,1,1,0,64\n",
                     ",".join(verify.CSV_FIELDS) + "\ntx,0,1\n"):
            with self.subTest(text=text):
                (self.directory / "packets.csv").write_text(text)
                self.assert_fails("packets:")
        self.make_fixture()
        for key, value in (("negotiated", "true"), ("tx", True), ("block", 7), ("state", None)):
            with self.subTest(key=key):
                self.make_fixture()
                self.status[key] = value
                self.save()
                summary = verify.verify(self.directory, self.status_path, self.library)
                self.assertFalse(summary["passed"])
                self.assertTrue(any("status:" in error for error in summary["errors"]))

    def test_cli_saves_complete_failed_summary(self):
        self.status["state"] = "failed"
        self.save()
        with mock.patch.object(verify, "reference_pcm", return_value=self.source[:, 0]), contextlib.redirect_stdout(io.StringIO()):
            result = verify.main(["--trace-dir", str(self.directory), "--status", str(self.status_path),
                                  "--reference-lib", str(self.library), "--output", str(self.output)])
        self.assertEqual(result, 1)
        summary = json.loads(self.output.read_text())
        self.assertFalse(summary["passed"])
        self.assertIsNotNone(summary["trace"])
        self.assertIsNotNone(summary["audio"])
        self.assertEqual(summary["status"]["state"], "failed")

    def test_cli_nonfinite_budget_saves_failure(self):
        for value in ("nan", "inf", "0", "-1"):
            with self.subTest(value=value), contextlib.redirect_stdout(io.StringIO()):
                result = verify.main(["--trace-dir", str(self.directory), "--status", str(self.status_path),
                                      "--reference-lib", str(self.library), "--output", str(self.output),
                                      "--steady-budget-ms", value])
                self.assertEqual(result, 1)
                self.assertFalse(json.loads(self.output.read_text())["passed"])

    def test_external_numeric_overflow_and_nonfinite_status_fail(self):
        for value in (1 << 64, 10 ** 30, "9" * 10000):
            with self.subTest(value=value):
                self.make_fixture()
                self.rows[-1]["rtt_us"] = value
                self.save()
                self.assert_fails("packets:")
        for value in ("NaN", "Infinity", "1e999"):
            with self.subTest(value=value):
                self.make_fixture()
                text = self.status_path.read_text()
                self.status_path.write_text(text[:-1] + ', "extra": ' + value + '}')
                with contextlib.redirect_stdout(io.StringIO()):
                    result = verify.main(["--trace-dir", str(self.directory), "--status", str(self.status_path),
                                          "--reference-lib", str(self.library), "--output", str(self.output)])
                self.assertEqual(result, 1)
                self.assertTrue(any("status:" in error for error in json.loads(self.output.read_text())["errors"]))

    def test_malformed_xrun_fails_at_status_boundary(self):
        for value in (1, None, [1], {}, {"capture_overrun": 0}):
            with self.subTest(value=value):
                self.make_fixture()
                self.status["xrun"] = value
                self.save()
                summary = verify.verify(self.directory, self.status_path, self.library)
                self.assertFalse(summary["passed"])
                self.assertTrue(any("status.xrun" in error for error in summary["errors"]))

    def test_long_json_integer_and_surrogate_save_failed_json(self):
        for extra in ('"tx": ' + "9" * 10000, '"extra": "\\ud800"'):
            with self.subTest(extra=extra[:30]):
                self.make_fixture()
                self.status["state"] = "failed"
                self.save()
                # 后一个 tx 字段覆盖原值，使转换和字段校验都接收超长输入。
                text = self.status_path.read_text()
                self.status_path.write_text(text[:-1] + ", " + extra + "}")
                with contextlib.redirect_stdout(io.StringIO()):
                    result = verify.main(["--trace-dir", str(self.directory), "--status", str(self.status_path),
                                          "--reference-lib", str(self.library), "--output", str(self.output)])
                self.assertEqual(result, 1)
                summary = json.loads(self.output.read_text())
                self.assertFalse(summary["passed"])
                self.assertTrue(any("status:" in error for error in summary["errors"]))

    def test_missing_artifacts_are_all_reported(self):
        for name in ("packets.csv", "capture.wav", "response.wav", "status.json"):
            (self.directory / name).unlink()
        summary = verify.verify(self.directory, self.status_path, self.library)
        self.assertFalse(summary["passed"])
        self.assertEqual(len(summary["errors"]), 4)

    def test_input_cannot_be_overwritten(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            verify.main(["--trace-dir", str(self.directory), "--status", str(self.status_path),
                         "--reference-lib", str(self.library), "--output", str(self.status_path)])
        self.assertEqual(json.loads(self.status_path.read_text()), self.status)

    def test_internal_reference_error_is_not_hidden(self):
        with mock.patch.object(verify, "reference_pcm", side_effect=RuntimeError("programming error")):
            with self.assertRaisesRegex(RuntimeError, "programming error"):
                verify.verify(self.directory, self.status_path, self.library)


class CReferenceTests(ArtifactFixture):
    @classmethod
    def setUpClass(cls):
        directory = ROOT / "build" / "run"
        directory.mkdir(parents=True, exist_ok=True)
        cls.build_tmp = tempfile.TemporaryDirectory(dir=directory)
        cls.addClassCleanup(cls.build_tmp.cleanup)
        configured = os.environ.get("MOZART_PRE_REFERENCE_LIB")
        if configured:
            cls.built_library = Path(configured).resolve()
        else:
            build = Path(cls.build_tmp.name)
            subprocess.run(["cmake", "-S", str(ROOT / "preprocessor"), "-B", str(build),
                            "-DMOZART_BUILD_TESTS=ON", "-DMOZART_USE_RNNOISE=OFF"],
                           check=True, capture_output=True, text=True)
            subprocess.run(["cmake", "--build", str(build), "--target", "mozart_pre_reference", "-j2"],
                           check=True, capture_output=True, text=True)
            cls.built_library = build / "libmozart_pre_reference.so"

    def independent_expected(self):
        # 直接调用 C API，用另一块长生成独立夹具，默认配置保持 denoise=true。
        lib = ctypes.CDLL(str(self.built_library))
        ptr = ctypes.POINTER(ctypes.c_float)
        lib.mozart_dsp_new.argtypes = [ctypes.c_void_p]
        lib.mozart_dsp_new.restype = ctypes.c_void_p
        lib.mozart_dsp_enhance.argtypes = [ctypes.c_void_p, ptr, ctypes.c_size_t, ptr, ctypes.c_size_t]
        lib.mozart_dsp_enhance.restype = ctypes.c_size_t
        lib.mozart_dsp_free.argtypes = [ctypes.c_void_p]
        dsp = lib.mozart_dsp_new(None)
        self.assertTrue(dsp)
        quantum = P["FAST_SAMPLES"] if self.status["block"] == P["FAST64_SAMPLES"] else P["FAST64_SAMPLES"]
        output = (ctypes.c_float * P["RATE"])()
        values = []
        try:
            for start in range(0, len(self.source) + P["RATE"], quantum):
                samples = (ctypes.c_float * quantum)()
                for i in range(quantum):
                    if start + i < len(self.source):
                        samples[i] = int(self.source[start + i, 0]) / 32768
                count = lib.mozart_dsp_enhance(dsp, samples, quantum, output, len(output))
                self.assertLessEqual(count, len(output))
                for i in range(count):
                    value = ctypes.c_float(max(-1, min(1, output[i])) * 32767).value
                    values.append(int(value))
                if len(values) >= len(self.source):
                    break
        finally:
            lib.mozart_dsp_free(dsp)
        self.assertGreaterEqual(len(values), len(self.source))
        return np.array(values[:len(self.source)], dtype=np.int16)

    def test_actual_c_reference_both_blocks_short_tail_and_short_session(self):
        for negotiated in (True, False):
            for warmup in (100, 0):
                with self.subTest(negotiated=negotiated, warmup=warmup):
                    self.make_fixture(negotiated=negotiated, warmup=warmup)
                    expected = self.independent_expected()
                    # 真值验证 FL 通道的高通和降噪结果。
                    self.assertGreater(int(np.max(np.abs(expected.astype(np.int32) - self.source[:, 0]))), 1)
                    write_wav(self.directory / "response.wav", np.column_stack((expected, expected)))
                    summary = verify.verify(self.directory, self.status_path, self.built_library)
                    if warmup:
                        self.assertTrue(summary["passed"], summary["errors"])
                    else:
                        self.assertFalse(summary["passed"])
                        self.assertTrue(any("status.warmup" in e for e in summary["errors"]))
                    self.assertEqual(summary["audio"]["max_error_lsb"], 0)
                    if warmup:
                        result = subprocess.run([
                            sys.executable, str(ROOT / "tools/verify_native_usb.py"),
                            "--trace-dir", str(self.directory), "--status", str(self.status_path),
                            "--reference-lib", str(self.built_library), "--output", str(self.output),
                        ], capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertTrue(json.loads(self.output.read_text())["passed"])
                    actual = np.column_stack((expected, expected))
                    actual[0, 1] += 2
                    write_wav(self.directory / "response.wav", actual)
                    self.assertFalse(verify.verify(self.directory, self.status_path, self.built_library)["passed"])

    def test_missing_reference_library_fails(self):
        summary = verify.verify(self.directory, self.status_path, self.library)
        self.assertFalse(summary["passed"])
        self.assertTrue(any("参考库加载失败" in error for error in summary["errors"]))


class ReferenceBoundaryTests(unittest.TestCase):
    def fake_lib(self, behavior):
        lib = mock.Mock()
        lib.mozart_dsp_new.return_value = 123
        lib.mozart_dsp_enhance.side_effect = behavior
        return lib

    def test_initial_configuration_clipping_float_s16_and_tail_zero_padding(self):
        blocks = []
        def enhance(dsp, samples, size, output, capacity):
            blocks.append([samples[i] for i in range(size)])
            for i, value in enumerate((-2.0, 2.0, 0.5, -0.5)):
                output[i] = value
            return 4
        lib = self.fake_lib(enhance)
        source = np.array([[32767, -32768], [0, 100], [-32768, 0]], dtype=np.int16)
        with mock.patch.object(verify.ctypes, "CDLL", return_value=lib):
            result = verify.reference_pcm(Path("fake.so"), source, P["FAST64_SAMPLES"])
        self.assertEqual(result.tolist(), [-32767, 32767, 16383])
        self.assertEqual(blocks[0][:3], [32767 / 32768, 0, -1])
        self.assertTrue(all(value == 0 for value in blocks[0][3:]))
        config = ctypes.cast(lib.mozart_dsp_new.call_args.args[0], ctypes.POINTER(verify.DspConfig)).contents
        self.assertTrue(config.denoise)
        self.assertFalse(config.gate or config.agc or config.limiter)
        lib.mozart_dsp_free.assert_called_once_with(123)

    def test_invalid_count_nonfinite_and_stalled_reference_fail_and_free(self):
        for kind in ("count", "nonfinite", "stalled"):
            with self.subTest(kind=kind):
                def enhance(dsp, samples, size, output, capacity):
                    if kind == "count":
                        return ctypes.c_size_t(-1).value
                    if kind == "nonfinite":
                        output[0] = float("nan")
                        return 1
                    return 0
                lib = self.fake_lib(enhance)
                with mock.patch.object(verify.ctypes, "CDLL", return_value=lib):
                    with self.assertRaises(verify.ArtifactError):
                        verify.reference_pcm(Path("fake.so"), np.zeros((1, P["CHANNELS"]), np.int16), P["FAST64_SAMPLES"])
                lib.mozart_dsp_free.assert_called_once_with(123)

    def test_allocation_failure(self):
        lib = self.fake_lib(lambda *args: 0)
        lib.mozart_dsp_new.return_value = None
        with mock.patch.object(verify.ctypes, "CDLL", return_value=lib):
            with self.assertRaisesRegex(verify.ArtifactError, "创建失败"):
                verify.reference_pcm(Path("fake.so"), np.zeros((1, P["CHANNELS"]), np.int16), P["FAST64_SAMPLES"])
        lib.mozart_dsp_free.assert_not_called()


if __name__ == "__main__":
    unittest.main()
