"""C 美化算子与独立滤波器参考的对拍测试。"""
import ctypes as C
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voice_polish_preview as P

LIBRARY = None


def setUpModule():
    global LIBRARY
    LIBRARY = P.load_library(P.build_library(P.ROOT / 'build/voice-polish/polish-test.so'))


class PolishNativeTest(unittest.TestCase):
    RATE = P.RATE

    @staticmethod
    def render(lib, audio, settings):
        return P.process(lib, np.ascontiguousarray(audio, np.float32), settings)[0]

    @staticmethod
    def settings(**overrides):
        base = dict(highpass_hz=0.0, low_peak_hz=220.0, low_peak_db=0.0, low_peak_q=1.0,
                    mid_peak_hz=500.0, mid_peak_db=0.0, mid_peak_q=1.2,
                    presence_hz=3000.0, presence_db=0.0, presence_q=0.8,
                    air_hz=8000.0, air_db=0.0, room_ms=0.0, room_mix=0.0,
                    gated_presence_db=0.0, gated_air_db=0.0, gate_ratio=4.0)
        base.update(overrides)
        return base

    @staticmethod
    def reference(audio, b, a):
        return signal.lfilter(b, a, audio.astype(np.float64))

    def assert_below_limiter(self, want):
        ceiling = P.limiter_ceiling(LIBRARY)
        self.assertLess(float(np.max(np.abs(want))), ceiling,
                        'reference example must not trigger the limiter')
        return want

    def response(self, settings, points):
        impulse = np.zeros(4800, dtype=np.float32)
        impulse[0] = 0.3
        output = self.render(LIBRARY, impulse, settings)
        spectrum = np.fft.rfft(output.astype(np.float64))
        reference = np.fft.rfft(impulse.astype(np.float64))
        gain = 20 * np.log10(np.maximum(np.abs(spectrum / reference), 1e-12))
        result = {}
        for hz, target in points:
            index = int(round(hz * impulse.size / self.RATE))
            result[hz] = (float(gain[index]), target)
        return result

    @staticmethod
    def peak(f0, gain_db, q, rate):
        A = 10 ** (gain_db / 40)
        w = 2 * math.pi * f0 / rate
        c, s = math.cos(w), math.sin(w)
        alpha = s / (2 * q)
        return (np.array([1 + alpha * A, -2 * c, 1 - alpha * A]) / (1 + alpha / A),
                np.array([1 + alpha / A, -2 * c, 1 - alpha / A]) / (1 + alpha / A))

    @staticmethod
    def highpass(f0, q, rate):
        w = 2 * math.pi * f0 / rate
        c, alpha = math.cos(w), math.sin(w) / (2 * q)
        a0 = 1 + alpha
        return (np.array([(1 + c) / 2, -(1 + c), (1 + c) / 2]) / a0,
                np.array([a0, -2 * c, 1 - alpha]) / a0)

    @staticmethod
    def high_shelf(f0, gain_db, rate):
        A = 10 ** (gain_db / 40)
        w = 2 * math.pi * f0 / rate
        c, s = math.cos(w), math.sin(w)
        beta = 2 * math.sqrt(A) * s / math.sqrt(2)
        a0 = (A + 1) - (A - 1) * c + beta
        return (np.array([A * ((A + 1) + (A - 1) * c + beta),
                          -2 * A * ((A - 1) + (A + 1) * c),
                          A * ((A + 1) + (A - 1) * c - beta)]) / a0,
                np.array([a0, 2 * ((A - 1) - (A + 1) * c),
                          (A + 1) - (A - 1) * c - beta]) / a0)

    def noise(self, seconds=0.5):
        rng = np.random.default_rng(20261027)
        return rng.normal(0, 0.1, round(self.RATE * seconds)).astype(np.float32)

    def test_bypass_is_bitwise_copy(self):
        audio = self.noise()
        output = self.render(LIBRARY, audio, self.settings())
        self.assertEqual(output.tobytes(), audio.tobytes())

    def test_peaking_matches_reference(self):
        audio = self.noise()
        got = self.render(LIBRARY, audio, self.settings(low_peak_db=-4.5))
        want = self.assert_below_limiter(
            self.reference(audio, *self.peak(220.0, -4.5, 1.0, self.RATE)))
        np.testing.assert_allclose(got, want, rtol=1e-5, atol=1e-6)

    def test_mid_peak_matches_reference(self):
        audio = self.noise()
        got = self.render(LIBRARY, audio,
                          self.settings(mid_peak_db=-2.0, mid_peak_q=1.2))
        want = self.assert_below_limiter(
            self.reference(audio, *self.peak(500.0, -2.0, 1.2, self.RATE)))
        np.testing.assert_allclose(got, want, rtol=1e-5, atol=1e-6)

    def test_presence_matches_reference(self):
        audio = self.noise()
        got = self.render(LIBRARY, audio, self.settings(presence_db=5.0, presence_hz=3200.0))
        want = self.assert_below_limiter(
            self.reference(audio, *self.peak(3200.0, 5.0, 0.8, self.RATE)))
        np.testing.assert_allclose(got, want, rtol=1e-5, atol=1e-6)

    def test_highpass_matches_reference(self):
        audio = self.noise()
        q = float(LIBRARY.polish_highpass_q())
        got = self.render(LIBRARY, audio, self.settings(highpass_hz=80.0))
        want = self.assert_below_limiter(
            self.reference(audio, *self.highpass(80.0, q, self.RATE)))
        np.testing.assert_allclose(got, want, rtol=1e-5, atol=1e-6)

    def test_air_shelf_matches_reference(self):
        audio = self.noise()
        got = self.render(LIBRARY, audio, self.settings(air_db=3.0))
        want = self.assert_below_limiter(
            self.reference(audio, *self.high_shelf(8000.0, 3.0, self.RATE)))
        np.testing.assert_allclose(got, want, rtol=1e-5, atol=1e-6)

    def test_frequency_response_matches_design_targets(self):
        """Independent of the coefficient transcription in both implementations."""
        cases = [
            (dict(low_peak_db=-4.5), 220.0, -4.5),
            (dict(mid_peak_db=-2.0), 500.0, -2.0),
            (dict(presence_db=5.0, presence_hz=3200.0), 3200.0, 5.0),
            (dict(air_db=3.0), 23000.0, 3.0),
            (dict(highpass_hz=80.0), 80.0, -3.0103),
        ]
        for settings, hz, target in cases:
            measured, _ = self.response(self.settings(**settings), [(hz, target)])[hz]
            self.assertLess(abs(measured - target), 0.05, f'{hz} Hz: {measured:.3f} vs {target:.3f}')
        for settings in ({'low_peak_db': -4.5}, {'mid_peak_db': -2.0},
                         {'presence_db': 5.0}, {'air_db': 3.0}):
            measured, _ = self.response(self.settings(**settings), [(0.0, 0.0)])[0.0]
            self.assertLess(abs(measured), 0.05, f'DC gain: {measured:.3f}')

    def test_filter_shape_matches_design_slopes(self):
        """Transition-band points expose Q and shelf slope, which the f0 gain hides."""
        transition = self.response(self.settings(presence_db=5.0, presence_hz=3200.0,
                                                 presence_q=0.8), [(1600.0, 2.018)])
        for hz, (measured, target) in transition.items():
            self.assertLess(abs(measured - target), 0.1, f'{hz} Hz: {measured:.3f} vs {target:.3f}')
        shelf = self.response(self.settings(air_db=3.0), [(16000.0, 2.963)])
        for hz, (measured, target) in shelf.items():
            self.assertLess(abs(measured - target), 0.1, f'{hz} Hz: {measured:.3f} vs {target:.3f}')

    def test_limiter_ceiling_matches_documented_value(self):
        """Independent of polish_limiter_ceiling: the chain uses -1 dBFS from mozart_limiter_init."""
        self.assertAlmostEqual(P.limiter_ceiling(LIBRARY), 10 ** (-1.0 / 20.0), places=6)

    def test_block_invariance(self):
        audio = self.noise()
        settings = P.PROFILES['03-bright']
        whole = self.render(LIBRARY, audio, settings)
        state = LIBRARY.polish_new(self.RATE, C.byref(P.Config(*[settings[n] for n in P.CONFIG_NAMES])))
        blocked = np.empty_like(audio)
        try:
            for start in range(0, audio.size, 1024):
                count = min(1024, audio.size - start)
                LIBRARY.polish_process(state, audio[start:start + count].ctypes.data_as(P.PTR),
                                       blocked[start:start + count].ctypes.data_as(P.PTR), count)
        finally:
            LIBRARY.polish_free(state)
        np.testing.assert_allclose(blocked, whole, rtol=0, atol=0)

    def test_room_first_echo_position(self):
        impulse = np.zeros(self.RATE, dtype=np.float32)
        impulse[0] = 0.5
        output = self.render(LIBRARY, impulse, self.settings(room_ms=23.0, room_mix=0.12))
        echoes = np.flatnonzero(np.abs(output[1:]) > 1e-5) + 1
        self.assertEqual(int(echoes[0]), round(23.0 * self.RATE / 1000.0))

    def test_in_place_is_safe(self):
        audio = self.noise().copy()
        settings = P.PROFILES['04-bright-room']
        state = LIBRARY.polish_new(self.RATE, C.byref(P.Config(*[settings[n] for n in P.CONFIG_NAMES])))
        in_place = audio.copy()
        try:
            LIBRARY.polish_process(state, in_place.ctypes.data_as(P.PTR),
                                   in_place.ctypes.data_as(P.PTR), in_place.size)
        finally:
            LIBRARY.polish_free(state)
        np.testing.assert_allclose(in_place, self.render(LIBRARY, audio, settings), rtol=0, atol=0)

    def test_reset_reproduces_output(self):
        audio = self.noise()
        settings = P.PROFILES['04-bright-room']
        config = P.Config(*[settings[n] for n in P.CONFIG_NAMES])
        state = LIBRARY.polish_new(self.RATE, C.byref(config))
        try:
            first = np.empty_like(audio)
            LIBRARY.polish_process(state, audio.ctypes.data_as(P.PTR), first.ctypes.data_as(P.PTR), audio.size)
            LIBRARY.polish_reset(state)
            second = np.empty_like(audio)
            LIBRARY.polish_process(state, audio.ctypes.data_as(P.PTR), second.ctypes.data_as(P.PTR), audio.size)
        finally:
            LIBRARY.polish_free(state)
        np.testing.assert_allclose(second, first, rtol=0, atol=0)

    def test_room_tail_is_finite_and_decays(self):
        impulse = np.zeros(self.RATE, dtype=np.float32)
        impulse[0] = 0.5
        output = self.render(LIBRARY, impulse, self.settings(room_ms=23.0, room_mix=0.12))
        self.assertTrue(np.isfinite(output).all())
        tail = np.abs(output[round(0.1 * self.RATE):])
        self.assertGreater(float(np.max(np.abs(output[round(0.02 * self.RATE):round(0.06 * self.RATE)]))), 1e-4)
        self.assertLess(float(np.max(tail)), 1e-4)

    def test_gated_highs_ignore_stationary_noise(self):
        """A stationary hiss has no low band modulation, so the gate stays closed."""
        rng = np.random.default_rng(20261027)
        audio = rng.normal(0, 0.05, self.RATE).astype(np.float32)
        output = self.render(LIBRARY, audio, P.PROFILES['06-extreme-gated'])
        quiet = slice(round(0.2 * self.RATE), round(0.9 * self.RATE))
        ratio = (np.sqrt(np.mean(output[quiet].astype(np.float64) ** 2)) /
                 np.sqrt(np.mean(audio[quiet].astype(np.float64) ** 2)))
        self.assertLess(abs(20 * np.log10(ratio)), 2.0)

    def test_gated_highs_open_on_modulated_speech_like_input(self):
        rng = np.random.default_rng(20261027)
        time_axis = np.arange(self.RATE) / self.RATE
        carrier = np.sin(2 * np.pi * 150.0 * time_axis)
        envelope = np.clip(np.sin(2 * np.pi * 3.0 * time_axis), 0.0, None) ** 2
        audio = (0.4 * carrier * envelope + rng.normal(0, 0.01, self.RATE)).astype(np.float32)
        output = self.render(LIBRARY, audio, P.PROFILES['06-extreme-gated'])
        plain = self.render(LIBRARY, audio, P.PROFILES['05-extreme-demo'])
        on = slice(round(0.35 * self.RATE), round(0.55 * self.RATE))
        def high_share(x):
            spectrum = np.abs(np.fft.rfft(x[on].astype(np.float64))) ** 2
            frequency = np.fft.rfftfreq(on.stop - on.start, 1 / self.RATE)
            return spectrum[(frequency >= 2000)].sum() / spectrum.sum()
        self.assertGreater(high_share(output), 0.5 * high_share(plain))

    def test_limiter_holds_ceiling(self):
        time_axis = np.arange(self.RATE // 2) / self.RATE
        audio = (2.0 * np.sin(2 * np.pi * 1000.0 * time_axis)).astype(np.float32)
        output = self.render(LIBRARY, audio, self.settings(presence_db=6.0))
        ceiling = P.limiter_ceiling(LIBRARY)
        self.assertTrue(np.isfinite(output).all())
        self.assertLessEqual(float(np.max(np.abs(output))), ceiling + 1e-6)
        self.assertGreaterEqual(float(np.max(np.abs(output))), ceiling - 1e-3)

    def test_profiles_produce_finite_output(self):
        audio = self.noise(seconds=1.0)
        for name, settings in P.PROFILES.items():
            output = self.render(LIBRARY, audio, settings)
            self.assertTrue(np.isfinite(output).all(), name)
            self.assertLessEqual(float(np.max(np.abs(output))), P.limiter_ceiling(LIBRARY) + 1e-6)

    def test_profiles_are_within_filter_bounds(self):
        for name, settings in P.PROFILES.items():
            if settings['room_mix'] > 0.0:
                self.assertGreaterEqual(
                    int(LIBRARY.polish_first_echo_samples(P.RATE, settings['room_ms'])), 1, name)
            for hz_key, q_key in (('highpass_hz', None), ('low_peak_hz', 'low_peak_q'),
                                  ('mid_peak_hz', 'mid_peak_q'),
                                  ('presence_hz', 'presence_q'), ('air_hz', None)):
                hz = settings[hz_key]
                if hz == 0.0:
                    continue
                self.assertLess(hz, P.RATE / 2, f'{name} {hz_key}')
                self.assertGreater(hz, 0.0, f'{name} {hz_key}')
                if q_key:
                    self.assertGreater(settings[q_key], 0.0, f'{name} {q_key}')

    def test_silence_stays_silent(self):
        audio = np.zeros(self.RATE // 10, dtype=np.float32)
        output = self.render(LIBRARY, audio, P.PROFILES['04-bright-room'])
        self.assertTrue(np.isfinite(output).all())
        self.assertEqual(float(np.max(np.abs(output))), 0.0)


class PolishPreviewTest(unittest.TestCase):
    def write(self, directory, rate=48000, dtype=np.int16, seconds=2.0, channels=1,
              amplitude=0):
        path = Path(directory) / 'input.wav'
        rng = np.random.default_rng(20261027)
        size = (round(rate * seconds), channels) if channels > 1 else round(rate * seconds)
        samples = (rng.normal(0, amplitude, size)).astype(dtype) if amplitude \
            else np.zeros(size, dtype=dtype)
        wavfile.write(path, rate, samples)
        return path

    def test_rejects_wrong_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'mono PCM16 16 kHz or 48 kHz'):
                P.read_source(self.write(directory, rate=44100))

    def test_sixteen_kilohertz_input_is_resampled(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write(directory, rate=16000, seconds=2.0, amplitude=2000)
            audio, rate, samples = P.read_source(source)
            self.assertEqual((rate, samples), (16000, 32000))
            self.assertEqual(audio.size, 96000)

    def test_rejects_float_and_short_input(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'mono PCM16 16 kHz or 48 kHz'):
                P.read_source(self.write(directory, dtype=np.float32))
            with self.assertRaisesRegex(ValueError, 'mono PCM16 16 kHz or 48 kHz'):
                P.read_source(self.write(directory, seconds=0.5))
            with self.assertRaisesRegex(ValueError, 'mono PCM16 16 kHz or 48 kHz'):
                P.read_source(self.write(directory, channels=2))

    def test_silent_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write(directory)
            with self.assertRaisesRegex(ValueError, 'nonzero active-frame energy'):
                P.main([str(source), str(Path(directory) / 'out')])

    def test_existing_output_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.write(directory, amplitude=2000)
            output = Path(directory) / 'out'
            output.mkdir()
            with self.assertRaises(FileExistsError):
                P.main([str(source), str(output)])

    def test_profiles_are_distinct_and_bounded(self):
        names = list(P.PROFILES)
        self.assertEqual(names, ['02-natural', '03-bright', '04-bright-room',
                                 '05-extreme-demo', '06-extreme-gated'])
        self.assertTrue(all(set(P.PROFILES[n]) == set(P.CONFIG_NAMES) for n in names))
        self.assertLess(P.PROFILES['04-bright-room']['room_mix'], 0.2)
        self.assertGreater(P.AB_SEGMENT_SECONDS, 0.0)
        self.assertGreater(P.AB_REPEATS, 0)


if __name__ == '__main__':
    unittest.main()
