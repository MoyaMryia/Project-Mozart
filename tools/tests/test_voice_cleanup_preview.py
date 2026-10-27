"""离线清理诊断、代理噪声剖面和电平匹配测试。"""
import io
import sys
import unittest
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voice_cleanup_preview import (RATE, SOURCE_RATE, diagnose, level_match,
    quiet_core_regions, read_source, source_masks, spectral_clean, rms)


class CleanupPreviewTests(unittest.TestCase):
    def test_source_format_is_checked_at_entry(self):
        for rate, data in [(48000, np.zeros(96000, np.int16)),
                           (16000, np.zeros(32000, np.float32)),
                           (16000, np.zeros(100, np.int16))]:
            with self.subTest(rate=rate, dtype=data.dtype, samples=len(data)):
                raw = io.BytesIO()
                wavfile.write(raw, rate, data)
                with self.assertRaisesRegex(ValueError, 'mono PCM16'):
                    read_source(raw.getvalue())

    def test_silence_and_uncalibrated_audio_fail(self):
        with self.assertRaisesRegex(ValueError, 'nonzero'):
            diagnose(np.zeros(SOURCE_RATE * 2))
        with self.assertRaisesRegex(ValueError, 'low-energy'):
            diagnose(np.ones(SOURCE_RATE * 2) * 0.1)

    def test_tone_candidates_preserve_fundamental_region(self):
        rate = SOURCE_RATE
        t = np.arange(rate * 4) / rate
        noise = .002 * np.sin(2 * np.pi * 50 * t) + .001 * np.sin(2 * np.pi * 200 * t)
        speech = np.zeros_like(t)
        speech[rate:rate * 3] = .05 * np.sin(2 * np.pi * 1000 * t[rate:rate * 3])
        report, quiet, active = diagnose(noise + speech)
        candidates = report['tone_decisions']
        self.assertTrue(any(x.get('notch_candidate') and abs(x['frequency_hz'] - 50) < 3
                            for x in candidates))
        fundamental = [x for x in candidates if abs(x.get('frequency_hz', 0) - 200) < 3]
        self.assertTrue(fundamental)
        self.assertFalse(fundamental[0]['notch_candidate'])
        self.assertFalse(report['quiet_frames_are_verified_noise'])
        self.assertGreater(quiet.sum(), 0)
        self.assertGreater(active.sum(), 0)

    def test_spectral_cleanup_improves_known_tone_snr(self):
        rng = np.random.default_rng(20261027)
        count = SOURCE_RATE * 4
        t = np.arange(count) / SOURCE_RATE
        clean = np.zeros(count)
        clean[SOURCE_RATE:3 * SOURCE_RATE] = .05 * np.sin(
            2 * np.pi * 1000 * t[SOURCE_RATE:3 * SOURCE_RATE])
        source = clean + rng.normal(0, .005, count)
        _, quiet, _ = diagnose(source)
        x = signal.resample_poly(source, 3, 1).astype(np.float32)
        expected = signal.resample_poly(clean, 3, 1)
        y, details = spectral_clean(x, quiet)
        self.assertEqual(len(y), len(x))
        self.assertTrue(np.isfinite(y).all())
        self.assertTrue(details['noise_profile_is_proxy'])
        region = slice(int(1.2 * RATE), int(2.8 * RATE))
        before = rms(x[region] - expected[region])
        after = rms(y[region] - expected[region])
        self.assertGreater(20 * np.log10(before / after), 3)
        self.assertLess(abs(20 * np.log10(rms(y[region]) / rms(expected[region]))), 1)

    def test_spectral_short_calibration_fails(self):
        with self.assertRaisesRegex(ValueError, 'calibration'):
            spectral_clean(np.zeros(RATE, np.float32), np.zeros(50, bool))

    def test_level_matching_preserves_active_rms(self):
        source = np.linspace(-.02, .02, 1024).astype(np.float32)
        active = np.arange(len(source)) % 2 == 0
        target = rms(source[active])
        result, gain = level_match(source * .25, active, target)
        self.assertAlmostEqual(gain, 4, places=5)
        self.assertLess(abs(20 * np.log10(rms(result[active]) / target)), 1e-5)

    def test_quiet_masks_exclude_partial_tail_and_short_regions(self):
        quiet = np.array([False, True, True, True, False, True, False])
        active = ~quiet
        mask, speech = source_masks(quiet, active, len(quiet) * 960 + 21)
        self.assertEqual(mask.sum(), quiet.sum() * 960)
        self.assertFalse(mask[-21:].any())
        self.assertFalse(speech[-21:].any())
        self.assertEqual(quiet_core_regions(quiet), [(2, 3)])


if __name__ == '__main__':
    unittest.main()
