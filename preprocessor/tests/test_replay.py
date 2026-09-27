"""Exercise WAV replay timing and UDP metadata using the real CLI, without audio hardware."""
import selectors
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path

BINARY = str(Path(sys.argv.pop(1)).resolve())
HEADER = struct.Struct('<IQIBBBB')


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='mozart-replay-')
        self.addCleanup(self.directory.cleanup)
        self.wav = Path(self.directory.name) / 'input.wav'
        with wave.open(str(self.wav), 'wb') as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(struct.pack('<1920h', *([1000] * 1920)))

    def listener(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.bind(('127.0.0.1', 0))
        listener.settimeout(3)
        self.addCleanup(listener.close)
        return listener

    def command(self, listener, *extra):
        return [BINARY, '-i', str(self.wav), '--no-rnnoise', '-a', '127.0.0.1',
                '-p', str(listener.getsockname()[1]), *extra]

    def start(self, command):
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate()
        self.addCleanup(cleanup)
        return process

    def test_paced_dual_send(self):
        first, second = self.listener(), self.listener()
        count = 25
        start = time.monotonic_ns()
        process = self.start(self.command(first, '--pace', '-n', str(count),
                                          '-b', f'127.0.0.1:{second.getsockname()[1]}'))
        packets = {first: [], second: []}
        with selectors.DefaultSelector() as selector:
            selector.register(first, selectors.EVENT_READ)
            selector.register(second, selectors.EVENT_READ)
            deadline = time.monotonic() + 5
            while min(map(len, packets.values())) < count and time.monotonic() < deadline:
                for key, _ in selector.select(.1):
                    packets[key.fileobj].append(key.fileobj.recv(4096))
                if process.poll() is not None and not selector.select(0):
                    break
        _, error = process.communicate(timeout=3)
        finish = time.monotonic_ns()
        self.assertEqual(process.returncode, 0, error.decode())
        self.assertEqual(len(packets[first]), count)
        self.assertEqual(packets[first], packets[second])
        self.assertGreaterEqual((finish-start)/1e9, .45)
        previous_pts = None
        for index, packet in enumerate(packets[first]):
            self.assertEqual(len(packet), 1300)
            magic, pts, frame, *_ = HEADER.unpack_from(packet)
            self.assertEqual(magic, 0x4D5A5254)
            self.assertEqual(frame, index+1)
            self.assertGreaterEqual(pts, start)
            self.assertLessEqual(pts, finish)
            if previous_pts is not None:
                self.assertEqual(pts-previous_pts, 20_000_000)
            previous_pts = pts

    def test_paced_replay_stops_on_signal(self):
        listener = self.listener()
        process = self.start(self.command(listener, '--pace'))
        self.assertEqual(len(listener.recv(4096)), 1300)
        process.send_signal(signal.SIGTERM)
        _, error = process.communicate(timeout=2)
        self.assertEqual(process.returncode, 0, error.decode())

    def test_pace_requires_file(self):
        result = subprocess.run([BINARY, '--pace', '--no-rnnoise'], capture_output=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'--pace requires -i', result.stderr)

    def test_fast_replay_preserves_relative_pts(self):
        listener = self.listener()
        process = self.start(self.command(listener, '-n', '2'))
        packets = [listener.recv(4096) for _ in range(2)]
        _, error = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 0, error.decode())
        self.assertEqual([HEADER.unpack_from(p)[1] for p in packets], [0, 20_000_000])


if __name__ == '__main__':
    unittest.main()
