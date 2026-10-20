from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tempo_player import TempoPlayer
from speech_service import SpeechService


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
class TempoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original = subprocess.Popen
        self.outputs = []
        def launch(command, **kwargs):
            if command[0] == 'aplay':
                path = self.root/f'output-{len(self.outputs)}.pcm'
                self.outputs.append(path)
                command = [sys.executable, '-c',
                    'import pathlib,sys;pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())', str(path)]
            return self.original(command, **kwargs)
        self.mock = patch('tempo_player.subprocess.Popen', side_effect=launch)
        self.mock.start()
        self.log = (self.root/'player.log').open('wb')
        self.players = []

    def tearDown(self):
        for player in self.players:
            SpeechService.kill(player)
        self.log.close()
        self.mock.stop()
        self.temp.cleanup()

    def player(self, device='test', tempo=1.1, rate=24000):
        player = TempoPlayer(rate, device, tempo, self.log, self.root/'capture.wav')
        self.players.append(player)
        return player

    def test_continuous_filter_keeps_tail_and_matches_whole_input(self):
        fixture = Path(__file__).resolve().parents[2]/'rvc-golden/tempo-frame-boundary-pocket.wav'
        with wave.open(str(fixture), 'rb') as source:
            pcm = source.readframes(source.getnframes())
        for tempo in [1.037, 1.1, 1.125, 1.15]:
            with self.subTest(tempo=tempo):
                expected = subprocess.run(['ffmpeg', '-v', 'error', '-f', 's16le', '-ar', '24000', '-ac', '1',
                    '-i', 'pipe:0', '-af', f'atempo={tempo:.3f}', '-f', 's16le', 'pipe:1'], input=pcm,
                    stdout=subprocess.PIPE, check=True).stdout
                for block_bytes in [2400, 4096, 8192]:
                    with self.subTest(block_bytes=block_bytes):
                        player = self.player(tempo=tempo)
                        library_path = Path(__file__).resolve().parents[2]/'build-gpu/preprocessor/libmozart-tempo.so'
                        self.assertEqual(player.backend, 'libavfilter_inprocess' if library_path.is_file() else 'ffmpeg')
                        for start in range(0, len(pcm), block_bytes):
                            player.stdin.write(pcm[start:start+block_bytes])
                        player.stdin.close()
                        self.assertEqual(player.wait(timeout=5), 0)
                        self.assertEqual(self.outputs[-1].read_bytes(), expected)
                        self.assertEqual(player.output_samples*2, len(expected))
                        self.assertIsNotNone(player.first_audio_at)
                        with wave.open(str(self.root/'capture.wav'), 'rb') as capture:
                            self.assertEqual(capture.readframes(capture.getnframes()), expected)

    def test_first_output_arrives_before_input_eof(self):
        player = self.player()
        player.stdin.write(b'\x10\x00'*12000)
        deadline = time.monotonic()+2
        while player.first_audio_at is None and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertIsNotNone(player.first_audio_at)
        self.assertFalse(player.stdin.closed)
        player.stdin.close()
        self.assertEqual(player.wait(timeout=3), 0)

    def test_pcm_frames_match_reference_at_supported_rates(self):
        fixture = Path(__file__).resolve().parents[2]/'rvc-golden/tempo-frame-boundary-pocket.wav'
        for rate in [8000, 16000, 22050, 24000, 32000, 44100, 48000]:
            with self.subTest(rate=rate):
                pcm = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(fixture),
                    '-ar', str(rate), '-f', 's16le', 'pipe:1'], stdout=subprocess.PIPE, check=True).stdout
                expected = subprocess.run(['ffmpeg', '-v', 'error', '-f', 's16le', '-ar', str(rate),
                    '-ac', '1', '-i', 'pipe:0', '-af', 'atempo=1.15', '-f', 's16le', 'pipe:1'],
                    input=pcm, stdout=subprocess.PIPE, check=True).stdout
                player = self.player(tempo=1.15, rate=rate)
                player.stdin.write(pcm)
                player.stdin.close()
                self.assertEqual(player.wait(timeout=5), 0)
                self.assertEqual(self.outputs[-1].read_bytes(), expected)

    def test_stop_reaps_filter_and_output_before_input_eof(self):
        player = self.player(device='null')
        player.stdin.write(b'\x10\x00'*12000)
        began = time.monotonic()
        SpeechService.kill(player)
        self.assertLess(time.monotonic()-began, .6)
        self.assertIsNotNone(player.filter.poll())
        self.assertIsNotNone(player.output.poll())
        self.assertFalse(player.reader.is_alive())

    def test_null_waits_for_actual_filtered_samples(self):
        player = self.player(device='null')
        began = time.monotonic()
        player.stdin.write(b'\x10\x00'*12000)
        player.stdin.close()
        self.assertEqual(player.wait(timeout=3), 0)
        elapsed = time.monotonic()-began
        self.assertGreaterEqual(elapsed, player.output_samples/24000)
        self.assertLess(elapsed, player.output_samples/24000+1)

    def test_failed_output_does_not_leave_filter_running(self):
        with patch('tempo_player.subprocess.Popen', side_effect=lambda command, **kwargs:
                self.original([sys.executable, '-c', 'raise SystemExit(7)'], **kwargs)
                if command[0] == 'aplay' else self.original(command, **kwargs)):
            player = self.player()
        try:
            player.stdin.write(b'\x10\x00'*12000)
            player.stdin.close()
        except BrokenPipeError:
            pass
        self.assertNotEqual(player.wait(timeout=3), 0)
        SpeechService.kill(player)
        self.assertIsNotNone(player.filter.poll())
        self.assertIsNotNone(player.output.poll())
        self.assertFalse(player.reader.is_alive())


if __name__ == '__main__':
    unittest.main()
