"""本机 C 均衡和保时长变调的数值回归。"""
import contextlib
import ctypes as C
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from scipy.io import wavfile
from scipy.signal import lfilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voice_dsp_preview import (SUBPROCESS_TIMEOUT_SECONDS, build_library, decode_wav,
                               load_library, main, pitch_preserve_duration)

PTR = C.POINTER(C.c_float)


class VoicePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.library = build_library(Path(cls.folder.name) / 'preview.so')
        cls.lib = load_library(cls.library)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def new_eq(self, low_db=0, presence_db=0):
        state = self.lib.voice_eq_new(48000, 250, low_db, 2500, presence_db, 0.7)
        self.assertTrue(state)
        self.addCleanup(self.lib.voice_eq_free, state)
        return state

    def process(self, state, samples):
        source = np.ascontiguousarray(samples, dtype=np.float32)
        output = np.empty_like(source)
        self.lib.voice_eq_process(state, source.ctypes.data_as(PTR),
                                  output.ctypes.data_as(PTR), len(source))
        return output

    def test_bypass_is_bitwise_equal(self):
        x = np.array([-0.0, 0.0, 0.99999994, -0.99999994, 0.75, -0.5], np.float32)
        self.assertEqual(self.process(self.new_eq(), x).tobytes(), x.tobytes())

    def test_arbitrary_chunks_match_single_call(self):
        x = (np.random.default_rng(20).standard_normal(12000) * 0.02).astype(np.float32)
        expected = self.process(self.new_eq(-2, 2), x)
        state = self.new_eq(-2, 2)
        chunks = [(0, 1), (1, 1777), (1778, 509), (2287, 4000), (6287, 5713)]
        actual = np.concatenate([self.process(state, x[i:i + count]) for i, count in chunks])
        np.testing.assert_array_equal(actual, expected)

    def test_reset_reproduces_output(self):
        state = self.new_eq(-2, 2)
        x = (np.sin(np.arange(6000) * 0.04) * 0.03).astype(np.float32)
        first = self.process(state, x)
        self.process(state, np.ones(200, np.float32) * 0.4)
        self.lib.voice_eq_reset(state)
        np.testing.assert_array_equal(self.process(state, x), first)

    def test_eq_matches_independent_rbj_reference(self):
        def rbj(x, hz, gain):
            a = 10 ** (gain / 40)
            w = 2 * np.pi * hz / 48000
            alpha = np.sin(w) / (2 * 0.7)
            denominator = 1 + alpha / a
            b = np.array([1 + alpha * a, -2 * np.cos(w), 1 - alpha * a]) / denominator
            coefficients = [1, -2 * np.cos(w) / denominator,
                            (1 - alpha / a) / denominator]
            return lfilter(b, coefficients, x)
        x = (np.random.default_rng(5).standard_normal(4096) * 0.001).astype(np.float32)
        expected = rbj(rbj(x.astype(np.float64), 250, -2), 2500, 2)
        actual = self.process(self.new_eq(-2, 2), x)
        self.assertLessEqual(float(np.max(np.abs(actual.astype(np.float64) - expected))), 1e-5)

    def test_pitch_frequency_and_duration(self):
        rate = 48000
        for hz in [120, 200, 440]:
            for shift in [-5, 5]:
                with self.subTest(hz=hz, shift=shift):
                    x = (0.1 * np.sin(2 * np.pi * hz * np.arange(rate * 2) / rate)).astype(np.float32)
                    y, _ = pitch_preserve_duration(x, shift)
                    self.assertEqual(len(y), len(x))
                    self.assertTrue(np.isfinite(y).all())
                    segment = y[rate // 4:]
                    spectrum = np.abs(np.fft.rfft(segment * np.hanning(len(segment))))
                    actual = np.fft.rfftfreq(len(segment), 1 / rate)[np.argmax(spectrum)]
                    target = hz * 2 ** (shift / 12)
                    self.assertLessEqual(abs(1200 * np.log2(actual / target)), 20)

    def test_pitch_timeout_stops_processing(self):
        with patch('voice_dsp_preview.subprocess.run',
                   side_effect=subprocess.TimeoutExpired('ffmpeg', SUBPROCESS_TIMEOUT_SECONDS)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                pitch_preserve_duration(np.zeros(480, np.float32), 5)
        self.assertEqual(run.call_args.kwargs['timeout'], SUBPROCESS_TIMEOUT_SECONDS)

    def test_high_level_is_finite_and_limited(self):
        y = self.process(self.new_eq(-2, 2), np.ones(2048, np.float32))
        self.assertEqual(y.size, 2048)
        self.assertTrue(np.isfinite(y).all())
        self.assertLessEqual(float(np.max(np.abs(y))), 10 ** (-1 / 20) + 1e-5)

    def test_decode_preserves_duration_and_channel_policy(self):
        raw = io.BytesIO()
        source = np.column_stack([np.full(160, 1000, np.int16),
                                  np.full(160, -1000, np.int16)])
        wavfile.write(raw, 16000, source)
        audio, rate, count = decode_wav(raw.getvalue())
        self.assertEqual((rate, count, len(audio)), (16000, 160, 480))
        self.assertGreater(float(np.mean(audio)), 0)

    def test_nonfinite_input_fails(self):
        raw = io.BytesIO()
        wavfile.write(raw, 48000, np.array([0.0, np.nan], np.float32))
        with self.assertRaisesRegex(ValueError, 'finite'):
            decode_wav(raw.getvalue())

    def test_unsupported_rate_fails(self):
        raw = io.BytesIO()
        wavfile.write(raw, 4000, np.zeros(40, np.int16))
        with self.assertRaisesRegex(ValueError, '8–192'):
            decode_wav(raw.getvalue())

    def source(self, directory):
        path = directory / 'source.wav'
        x = (1000 * np.sin(2 * np.pi * 200 * np.arange(2000) / 16000)).astype(np.int16)
        wavfile.write(path, 16000, x)
        return path

    def test_cli_preserves_source_bytes_and_output_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = self.source(directory)
            raw = source.read_bytes()
            output = directory / 'preview'
            with patch('voice_dsp_preview.build_library', return_value=self.library), \
                    patch('voice_dsp_preview.listening_file', return_value={}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([str(source), str(output)]), 0)
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual((output / '01-original.wav').read_bytes(), raw)
            report = json.loads((output / 'manifest.json').read_text())
            self.assertEqual(report['input_sha256'], hashlib.sha256(raw).hexdigest())
            for result in report['outputs'].values():
                self.assertEqual(result['samples'], 6000)
            self.assertGreater(report['preprocessing']['first_emit_input_samples'], 0)

    def test_cli_refuses_existing_output_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = self.source(directory)
            output = directory / 'preview'
            output.mkdir()
            marker = output / 'keep.txt'
            marker.write_text('keep')
            with self.assertRaises(FileExistsError):
                main([str(source), str(output)])
            self.assertEqual(marker.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
