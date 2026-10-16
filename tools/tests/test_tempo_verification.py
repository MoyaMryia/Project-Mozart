import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'rvc-golden'))
import verify_tempo_playback


class PlaybackVerificationTests(unittest.TestCase):
    def verify(self, actual, native, ffmpeg, *, rate=24000, samples=None, helper=True, cli=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / 'speech/results'
            results.mkdir(parents=True)
            for filename, data, sample_rate in [('job.wav', b'\x01\x00' * 4, 24000),
                                                 ('playback.wav', actual, rate)]:
                with wave.open(str(results / filename), 'wb') as output:
                    output.setparams((1, 2, sample_rate, 0, 'NONE', 'not compressed'))
                    output.writeframes(data)
            (root / 'jobs.json').write_text(json.dumps([dict(
                id='job', playback_audio_filename='playback.wav', playback_tempo=1.15,
                playback_tempo_backend='libavfilter_inprocess',
                playback_output_samples=len(actual) // 2 if samples is None else samples,
                playback_started_at=2., playback_input_started_at=1., duration_seconds=1.)]))
            output = root / 'verification.json'
            argv = ['verify_tempo_playback.py', '--replay', str(root), '--output', str(output)]
            if helper:
                argv += ['--helper', 'test-native-helper']
            if cli:
                result = subprocess.run([sys.executable, verify_tempo_playback.__file__, *argv[1:]],
                                        capture_output=True, text=True, timeout=15)
                return result.returncode, json.loads(output.read_text())
            process = MagicMock(returncode=0)
            process.poll.return_value = 0
            process.stdout.read.return_value = b'\x01\x00'

            def reference(command, **kwargs):
                return MagicMock(stdout=ffmpeg if command[0] == 'ffmpeg' else native)

            error = None
            with patch.object(sys, 'argv', argv), \
                    patch.object(verify_tempo_playback.subprocess, 'Popen', return_value=process), \
                    patch.object(verify_tempo_playback.subprocess, 'run', side_effect=reference), \
                    contextlib.redirect_stdout(io.StringIO()):
                try:
                    verify_tempo_playback.main()
                except RuntimeError as caught:
                    error = caught
            return error, json.loads(output.read_text())

    def test_native_self_agreement_does_not_replace_ffmpeg_reference(self):
        actual = b'\x01\x00' * 4
        error, result = self.verify(actual, actual, b'\x02\x00' * 4)
        self.assertIsNotNone(error)
        check = result['playback']['checks'][0]
        self.assertTrue(check['exact'])
        self.assertFalse(check['ffmpeg_exact'])
        self.assertEqual(check['ffmpeg_sample_difference'], 0)

    def test_native_disagreement_fails_even_when_ffmpeg_matches(self):
        actual = b'\x01\x00' * 4
        error, result = self.verify(actual, b'\x02\x00' * 4, actual)
        self.assertIsNotNone(error)
        self.assertFalse(result['playback']['checks'][0]['exact'])
        self.assertTrue(result['playback']['checks'][0]['ffmpeg_exact'])

    def test_matching_independent_references_pass(self):
        actual = b'\x01\x00' * 4
        for helper in (True, False):
            with self.subTest(helper=helper):
                error, result = self.verify(actual, actual, actual, helper=helper)
                self.assertIsNone(error)
                self.assertTrue(result['playback']['checks'][0]['ffmpeg_exact'])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
    def test_cli_exits_nonzero_on_ffmpeg_disagreement(self):
        code, result = self.verify(b'\x02\x00' * 4, None, None, helper=False, cli=True)
        self.assertNotEqual(code, 0)
        self.assertFalse(result['playback']['checks'][0]['ffmpeg_exact'])

    def test_existing_rate_and_sample_count_checks_still_fail(self):
        actual = b'\x01\x00' * 4
        for options in (dict(rate=16000), dict(samples=3)):
            with self.subTest(options=options):
                error, _ = self.verify(actual, actual, actual, **options)
                self.assertIsNotNone(error)


if __name__ == '__main__':
    unittest.main()
