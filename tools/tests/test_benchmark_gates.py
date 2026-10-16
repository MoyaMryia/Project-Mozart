import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'rvc-golden'))

import benchmark_speech_blocks
import collect_feature_latency
import run_efficiency_campaign


class FeatureLatencyCompareTests(unittest.TestCase):
    def test_preserves_valid_metrics(self):
        left = np.array([1.0, 2.0])
        right = np.array([1.0, 1.0])
        result = collect_feature_latency.compare(left, right)
        self.assertEqual(result['samples'], 2)
        self.assertFalse(result['exact'])
        self.assertAlmostEqual(result['relative_rmse'], 1 / np.sqrt(5))
        self.assertAlmostEqual(result['cosine'], 3 / np.sqrt(10))
        self.assertEqual(result['max_absolute_error'], 1.0)

    def test_rejects_empty_and_zero_norm_audio(self):
        with self.assertRaises(ValueError):
            collect_feature_latency.compare(np.array([]), np.array([]))
        zero = np.zeros(2)
        nonzero = np.ones(2)
        with self.assertRaises(ValueError):
            collect_feature_latency.compare(zero, zero)
        with self.assertRaises(ValueError):
            collect_feature_latency.compare(zero, nonzero)
        with self.assertRaises(ValueError):
            collect_feature_latency.compare(nonzero, zero)

    def test_rejects_non_finite_audio(self):
        for invalid in (np.nan, np.inf, -np.inf):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                collect_feature_latency.compare(np.array([invalid]), np.ones(1))
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                collect_feature_latency.compare(np.ones(1), np.array([invalid]))

    def test_rejects_overflow_from_finite_audio(self):
        with self.assertRaises(ValueError):
            collect_feature_latency.compare(np.array([1e308]), np.array([-1e308]))


class CampaignCaseTests(unittest.TestCase):
    def invoke(self, cases, output):
        argv = ['run_efficiency_campaign.py', '--baseline', 'baseline-bin', '--candidate', 'candidate-bin',
                '--repo', '.', '--assets', str(output / 'missing-assets'), '--input', 'input.wav',
                '--output-dir', str(output), '--cases', *cases]
        with mock.patch.object(sys, 'argv', argv):
            run_efficiency_campaign.main()

    def test_rejects_baseline_only_before_creating_output(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'output'
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.invoke(['baseline'], output)
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(output.exists())

    def test_rejects_missing_baseline_before_creating_output(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'output'
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.invoke(['compatible', 'chain'], output)
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(output.exists())

    def test_duplicate_baseline_does_not_count_as_second_case(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'output'
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.invoke(['baseline', 'baseline'], output)
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(output.exists())

    def test_valid_cases_reach_asset_traversal(self):
        class ReachedAssetTraversal(Exception):
            pass

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'output'
            with mock.patch.object(Path, 'iterdir', side_effect=ReachedAssetTraversal), self.assertRaises(ReachedAssetTraversal):
                self.invoke(['baseline', 'compatible'], output)
            self.assertTrue(output.exists())


class SpeechBlockAudioTests(unittest.TestCase):
    def test_rejects_empty_decoded_audio(self):
        with self.assertRaises(ValueError):
            benchmark_speech_blocks.compare_audio(np.array([]), np.array([]))
        with self.assertRaises(ValueError):
            benchmark_speech_blocks.compare_audio(np.ones(1), np.array([]))

    def test_rejects_non_finite_decoded_audio(self):
        for invalid in (np.nan, np.inf, -np.inf):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                benchmark_speech_blocks.compare_audio(np.array([invalid]), np.ones(1))
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                benchmark_speech_blocks.compare_audio(np.ones(1), np.array([invalid]))

    def test_rejects_overflow_from_finite_decoded_audio(self):
        for value, reference in [(np.array([1e308]), np.ones(1)),
                                 (np.ones(1), np.array([1e308])),
                                 (np.array([1e308]), np.array([-1e308]))]:
            with self.subTest(value=value, reference=reference), self.assertRaises(ValueError):
                benchmark_speech_blocks.compare_audio(value, reference)

    def test_preserves_audio_comparison_semantics(self):
        comparison = benchmark_speech_blocks.compare_audio(np.array([1.0, 2.0]), np.array([1.0, 1.0]))
        self.assertFalse(comparison['exact'])
        self.assertAlmostEqual(comparison['relative_rmse'], 1 / np.sqrt(2))


if __name__ == '__main__':
    unittest.main()
