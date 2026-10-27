import ctypes
import pathlib
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from scipy import signal

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_cleanup_preview import Config, STAT_NAMES, bind_cleanup_api
from voice_dsp_preview import SUBPROCESS_TIMEOUT_SECONDS, RATE


def config(**values):
    defaults = dict(notch_1_hz=0, notch_2_hz=0, notch_3_hz=0, notch_q=30,
                    lowpass_hz=0, gate_open_db=-20, gate_close_db=-30,
                    gate_floor_db=0, gate_hold_ms=100, gate_attack_ms=5,
                    gate_release_ms=80, plosive_max_db=0, plosive_ratio=4)
    defaults.update(values)
    return Config(*(defaults[name] for name, _ in Config._fields_))


class NativeCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        so = pathlib.Path(cls.tmp.name) / "voice_cleanup.so"
        subprocess.run(["cc", "-std=c11", "-O2", "-fPIC", "-shared",
                        str(ROOT / "voice_cleanup.c"), "-lm", "-o", str(so)],
                       check=True, timeout=SUBPROCESS_TIMEOUT_SECONDS)
        cls.lib = bind_cleanup_api(ctypes.CDLL(str(so)))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_native(self, x, cfg):
        handle = self.lib.cleanup_new(RATE, ctypes.byref(cfg))
        self.assertTrue(handle)
        x = np.asarray(x, dtype=np.float32)
        y = np.empty_like(x)
        self.lib.cleanup_process(handle, x.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                                 y.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), len(x))
        return handle, y

    def test_notch_and_lowpass_match_scipy(self):
        rng = np.random.default_rng(7)
        x = rng.normal(size=4096).astype(np.float32) * 0.1
        cfg = config(notch_1_hz=60, notch_q=30, lowpass_hz=8000)
        h, y = self.run_native(x, cfg)
        try:
            b, a = signal.iirnotch(60, 30, fs=RATE)
            expected = signal.lfilter(b, a, x)
            sos = signal.butter(4, 8000, btype="low", fs=RATE, output="sos")
            expected = signal.sosfilt(sos, expected)
            np.testing.assert_allclose(y, expected, rtol=1e-5, atol=1e-5)
        finally:
            self.lib.cleanup_free(h)

    def test_notch_suppresses_60_and_preserves_1k(self):
        t = np.arange(RATE, dtype=np.float64) / RATE
        x = (np.sin(2*np.pi*60*t) + np.sin(2*np.pi*1000*t)).astype(np.float32)
        h, y = self.run_native(x, config(notch_1_hz=60, notch_q=30))
        try:
            self.assertLess(np.sqrt(np.mean(y[12000:]**2)), 0.72)
            self.assertGreater(np.sqrt(np.mean(y[12000:]**2)), 0.68)
        finally:
            self.lib.cleanup_free(h)

    def test_gate_floor_opening_and_tail_hold(self):
        rng = np.random.default_rng(3)
        x = np.concatenate([rng.normal(0, .001, RATE//2), np.ones(RATE//4)*.2,
                            np.ones(RATE//4)*.001]).astype(np.float32)
        cfg = config(gate_open_db=-30, gate_close_db=-40, gate_floor_db=-24,
                     gate_hold_ms=100, gate_attack_ms=5, gate_release_ms=80)
        h, y = self.run_native(x, cfg)
        try:
            self.assertLess(np.sqrt(np.mean(y[5000:15000]**2)), .0003)
            self.assertGreater(np.sqrt(np.mean(y[26000:33000]**2)), .15)
            # 低电平尾段在保持时间内继续通过。
            self.assertGreater(np.mean(np.abs(y[36000:39000])), .0008)
        finally:
            self.lib.cleanup_free(h)

    def test_plosive_initial_state_and_reset_match(self):
        t = np.arange(RATE // 2, dtype=np.float64) / RATE
        x = np.concatenate([np.zeros(RATE // 4), .1 * np.sin(2 * np.pi * 45 * t)]).astype(np.float32)
        cfg = config(plosive_max_db=-9, plosive_ratio=4)
        h, first = self.run_native(x, cfg)
        try:
            stats = (ctypes.c_double * len(STAT_NAMES))()
            self.lib.cleanup_get_stats(h, stats)
            self.assertEqual(stats[0], len(x))
            self.assertAlmostEqual(stats[-1], float(np.max(np.abs(first))), places=6)
            self.assertGreater(stats[2], 0)
            self.assertLess(stats[4], .9)
            self.assertGreaterEqual(stats[4], 10 ** (cfg.plosive_max_db / 20) - 1e-6)
            self.lib.cleanup_reset(h)
            again = np.empty_like(x)
            self.lib.cleanup_process(h, x.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                                     again.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), len(x))
            np.testing.assert_array_equal(first, again)
        finally:
            self.lib.cleanup_free(h)

    def test_chunk_reset_bypass_and_impulse(self):
        rng = np.random.default_rng(9)
        x = rng.normal(0, .1, 12000).astype(np.float32)
        cfg = config(notch_1_hz=60, notch_q=20, lowpass_hz=9000)
        h, whole = self.run_native(x, cfg)
        try:
            self.lib.cleanup_reset(h)
            split = np.empty_like(x)
            for start in range(0, len(x), 317):
                n = min(317, len(x)-start)
                self.lib.cleanup_process(h, x[start:].ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                                         split[start:].ctypes.data_as(ctypes.POINTER(ctypes.c_float)), n)
            np.testing.assert_array_equal(whole, split)
            self.lib.cleanup_reset(h)
            again = np.empty_like(x)
            self.lib.cleanup_process(h, x.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                                     again.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), len(x))
            np.testing.assert_array_equal(whole, again)
        finally:
            self.lib.cleanup_free(h)
        h, exact = self.run_native(x, config())
        try:
            self.assertEqual(exact.tobytes(), x.tobytes())
        finally:
            self.lib.cleanup_free(h)
        impulse = np.zeros(2048, dtype=np.float32); impulse[0] = 1.0
        h, out = self.run_native(impulse, config(notch_1_hz=60, notch_q=30,
                                                  plosive_max_db=-9, plosive_ratio=4))
        try:
            self.assertEqual(len(out), len(impulse))
            self.assertTrue(np.isfinite(out).all())
        finally:
            self.lib.cleanup_free(h)


if __name__ == "__main__":
    unittest.main()
