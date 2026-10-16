import copy
import json
import re
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speech_duration import DEFAULT_PROFILE, DurationEstimator, duration_features, source_duration
from tempo_control import MAX_TEMPO, TempoController


class DurationTests(unittest.TestCase):
    def setUp(self):
        self.estimator = DurationEstimator()
        self.profile = json.loads(DEFAULT_PROFILE.read_text())

    def predict(self, text, **options):
        return self.estimator.predict([text], options.get('language', 'en'),
            options.get('revision', self.profile['voice_revision']), options.get('runtime', self.profile['runtime']))

    def test_voice_and_actual_runtime_assets_bound_calibration_scope(self):
        self.assertEqual(self.predict('Please bring the documents.')['method'], 'tiny_linear_v1')
        for options in [dict(language='zh'), dict(revision='different'), dict(runtime=None)]:
            self.assertEqual(self.predict('Please bring the documents.', **options)['method'], 'heuristic')
        runtime = copy.deepcopy(self.profile['runtime'])
        name = next(iter(runtime['model_sha256']))
        runtime['model_sha256'][name] = 'different'
        self.assertEqual(self.predict('Please bring the documents.', runtime=runtime)['method'], 'heuristic')

    def test_numerals_abbreviations_and_out_of_range_text_use_fallback(self):
        for text in ['Pay 25,000 yuan.', 'The CPU needs more memory.', '你好。', 'Hello.', 'word '*100]:
            with self.subTest(text=text):
                result = self.predict(text)
                self.assertEqual(result['method'], 'heuristic')
                self.assertGreaterEqual(result['reserve_seconds'], .8)
        self.assertGreaterEqual(self.predict('Pay 25,000 yuan.')['seconds'], 2.16)

    def test_each_synthesis_part_keeps_its_own_pause_and_uncertainty(self):
        parts = ['Please close the window.', 'Bring the documents.']
        combined = self.estimator.predict(parts, 'en', self.profile['voice_revision'], self.profile['runtime'])
        singles = [self.predict(t) for t in parts]
        self.assertAlmostEqual(combined['seconds'], sum(p['seconds'] for p in singles))
        self.assertGreater(combined['reserve_seconds'], combined['seconds'])

    def test_invalid_and_missing_profiles_do_not_stop_speech(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'profile.json'
            for data in [{}, {**self.profile, 'coefficients': [float('nan')]*4},
                         {**self.profile, 'min_words': 0}, {**self.profile, 'max_words': 'bad'}]:
                path.write_text(json.dumps(data))
                estimator = DurationEstimator(path)
                self.assertIsNone(estimator.profile)
                self.assertEqual(estimator.predict(['Hello.'], 'en', '', None)['method'], 'heuristic')
            path.unlink()
            self.assertIsNone(DurationEstimator(path).profile)

    def test_observed_interval_has_priority_and_clauses_cannot_reuse_parent(self):
        event = dict(audio_start_pts_ns=1_000_000_000, audio_end_pts_ns=3_000_000_000, source_audio_seconds=10)
        self.assertEqual(source_duration(event), 2)
        self.assertIsNone(source_duration({**event, 'clause_parent_id': 'p'}))
        self.assertEqual(source_duration({**event, 'clause_parent_id': 'p', 'source_duration_seconds': 1}), 1)
        self.assertIsNone(source_duration({**event, 'source_correction': True}))
        for value in [float('nan'), float('inf'), -1, 0, True, '2']:
            self.assertIsNone(source_duration({'source_duration_seconds': value}))


class TempoControlTests(unittest.TestCase):
    def test_ratio_cap_and_unserved_time_are_explicit(self):
        control = TempoController()
        result = control.select(3, 2, .5, 3, 'voice', 100)
        self.assertEqual(result['source_ratio'], 1.5)
        self.assertEqual(result['tempo'], 1.15)
        self.assertTrue(result['limited'])

    def test_continuous_ratio_and_feedback_do_not_require_source_timing(self):
        control = TempoController()
        result = control.select(2.074, 2, 0, 2, 'voice', 100)
        self.assertEqual(result['tempo'], 1.037)
        result = control.select(2, None, 2.4, 2, 'voice', 101)
        self.assertEqual(result['tempo'], 1.03)
        self.assertAlmostEqual(result['recovery_ratio'], 1.03)

    def test_release_is_gradual_and_idle_and_other_voice_reset_history(self):
        control = TempoController(1.05)
        self.assertEqual(control.select(2, 1, 0, 2, 'a', 100)['tempo'], 1.05)
        self.assertEqual(control.select(2, 3, 0, 2, 'a', 101)['tempo'], 1.04)
        self.assertEqual(control.select(2, 3, 0, 2, 'b', 101)['tempo'], 1)
        self.assertEqual(control.select(2, 3, 0, 2, 'a', 107)['tempo'], 1)

    def test_invalid_limits_and_inputs_fail_before_audio(self):
        for maximum in [float('nan'), .9, 1.1501, 1.2]:
            with self.assertRaises(ValueError):
                TempoController(maximum)
        self.assertLessEqual(TempoController(1.0374).select(3, 2, 0, 3, 'a', 100)['tempo'], 1.0374)

    def test_python_and_native_upper_limits_match(self):
        source = (Path(__file__).resolve().parents[2]/'preprocessor/src/tempo_filter.c').read_text()
        value = re.search(r'^#define MOZART_MAX_TEMPO ([0-9.]+)$', source, re.MULTILINE)
        self.assertIsNotNone(value)
        self.assertEqual(float(value.group(1)), MAX_TEMPO)

    def test_extended_cap_keeps_intermediate_ratios_and_gradual_release(self):
        control = TempoController()
        self.assertEqual(control.select(3, 2, 0, 3, 'a', 100)['tempo'], 1.15)
        self.assertEqual(control.select(2, 3, 0, 2, 'a', 101)['tempo'], 1.14)
        self.assertEqual(control.select(2.25, 2, 0, 2.25, 'b', 101)['tempo'], 1.125)
        self.assertEqual(control.select(2, 3, 0, 2, 'a', 107)['tempo'], 1)


if __name__ == '__main__':
    unittest.main()
