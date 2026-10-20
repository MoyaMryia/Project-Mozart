import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'rvc-golden'))
from analyze_tempo_limit import collect


class ReportGateTests(unittest.TestCase):
    def paired_files(self, root, different_input=False):
        for name, delay, source in [('final105', 3., 'a'*64),
                                    ('final115', 1., ('b' if different_input else 'a')*64)]:
            case = root/name
            case.mkdir()
            analysis = dict(protocol={'source_sha256': source},
                parents=[{'first_play_minus_final_emit_seconds': delay}], parent_count=1,
                completed_speech_parents=1, minimum_available_GiB=3., native_frames=6000,
                native_overruns=0, source_covered_parents=1, playback_order_preserved=True,
                revision_violations=[], final_speech_status={}, exit={})
            files = {'speech-analysis.json': analysis,
                'jobs.json': [dict(id='job', status='completed', playback_audio_filename='play.wav',
                    playback_tempo=1.15, duration_seconds=2., tempo_decision={'limited': False})],
                'verification.json': {'playback': {'total_playback_seconds': 1., 'checks': [dict(
                    job='job', exact=True, ffmpeg_exact=True, native_exact=True, rate_matches=True, metadata_matches=True)]}},
                'campaign.json': {'comparison_performed': False, 'all_pcm_identical': None, 'runs': [{}]},
                'source-signatures.json': [{'wav_sha256': 'same'}]}
            for filename, value in files.items():
                (case/filename).write_text(json.dumps(value))

    def test_different_source_input_fails_even_when_text_and_synthesis_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.paired_files(root, different_input=True)
            with self.assertRaisesRegex(ValueError, 'Source audio SHA-256 changed'):
                collect(root)

    def test_matching_inputs_and_strict_checks_produce_gain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.paired_files(root)
            result = collect(root)
            self.assertAlmostEqual(result['median_reduction'], 2/3)

    def test_duplicated_verification_does_not_replace_a_missing_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.paired_files(root)
            case = root/'final115'
            jobs = json.loads((case/'jobs.json').read_text())
            jobs.append({**jobs[0], 'id': 'other'})
            (case/'jobs.json').write_text(json.dumps(jobs))
            verification = json.loads((case/'verification.json').read_text())
            verification['playback']['checks'] *= 2
            (case/'verification.json').write_text(json.dumps(verification))
            with self.assertRaisesRegex(ValueError, 'Independent playback verification failed'):
                collect(root)

    def test_nonfinite_parent_timing_fails_before_reporting_gain(self):
        for delay in [float('nan'), float('inf'), None]:
            with self.subTest(delay=delay), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                case = root/'final105'
                case.mkdir()
                files = {
                    'speech-analysis.json': {'parents': [{'first_play_minus_final_emit_seconds': delay}]},
                    'jobs.json': [{'id': 'job', 'status': 'completed', 'playback_audio_filename': 'play.wav', 'playback_tempo': 1.15}],
                    'verification.json': {'playback': {'checks': [dict(job='job', exact=True, ffmpeg_exact=True,
                        native_exact=True, rate_matches=True, metadata_matches=True)]}},
                    'campaign.json': {'comparison_performed': False, 'all_pcm_identical': None},
                    'source-signatures.json': [{'wav_sha256': 'same'}],
                }
                for name, value in files.items():
                    (case/name).write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, 'non-finite parent playback timestamps'):
                    collect(root)
