"""Exercise WAV replay timing and UDP metadata using the real CLI, without audio hardware."""
import errno
import os
import selectors
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
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

    def start(self, command, env=None):
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
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


class AlsaFailureTest(unittest.TestCase):
    setUp = ReplayTest.setUp
    listener = ReplayTest.listener
    command = ReplayTest.command
    start = ReplayTest.start
    @classmethod
    def setUpClass(cls):
        cls.shim_directory = tempfile.TemporaryDirectory(prefix='mozart-alsa-faults-')
        cls.addClassCleanup(cls.shim_directory.cleanup)
        cls.shim = Path(cls.shim_directory.name) / 'alsa_faults.so'
        subprocess.run([os.environ.get('CC', 'cc'), '-shared', '-fPIC',
                        str(Path(__file__).with_name('alsa_faults.c')), '-o', str(cls.shim), '-ldl'],
                       check=True, capture_output=True)

    def echo_server(self):
        listener = self.listener()
        listener.settimeout(.1)
        stopped = threading.Event()
        def echo():
            while not stopped.is_set():
                try:
                    packet, peer = listener.recvfrom(4096)
                except socket.timeout:
                    continue
                listener.sendto(packet[:HEADER.size] + bytes(960 * 4), peer)
        worker = threading.Thread(target=echo)
        worker.start()
        def cleanup():
            stopped.set()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.addCleanup(cleanup)
        return listener

    def fault_process(self, direction, mode, finite=True):
        listener = self.echo_server() if direction == 'write' else self.listener()
        if direction == 'write':
            command = self.command(listener, '--pace', '-o', 'null')
        else:
            command = [BINARY, '-d', 'null', '--no-rnnoise',
                       '-p', str(listener.getsockname()[1])]
        if finite:
            command += ['-n', '1']
        env = dict(os.environ, LD_PRELOAD=str(self.shim))
        env[f'MOZART_FAULT_{direction.upper()}'] = mode
        return self.start(command, env)

    def wait_marker(self, process, marker):
        data = b''
        deadline = time.monotonic() + 2
        with selectors.DefaultSelector() as selector:
            selector.register(process.stderr, selectors.EVENT_READ)
            while marker not in data and time.monotonic() < deadline:
                if selector.select(.1):
                    block = os.read(process.stderr.fileno(), 4096)
                    if not block:
                        break
                    data += block
        self.assertIn(marker, data)
        return data

    def test_requested_output_open_failure_stops_before_input(self):
        listener = self.listener()
        process = self.start(self.command(listener, '-o', 'mozart_missing_output', '-n', '1'))
        _, error = process.communicate(timeout=2)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b'status=failed stage=open', error)
        listener.settimeout(.1)
        with self.assertRaises(socket.timeout):
            listener.recv(4096)

    def test_playback_write_failure_stops_unbounded_input(self):
        process = self.fault_process('write', 'device_gone', finite=False)
        _, error = process.communicate(timeout=2)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b'[fault] write', error)
        self.assertIn(b'status=failed stage=write', error)

    def test_playback_failure_interrupts_capture_wait(self):
        listener = self.echo_server()
        env = dict(os.environ, LD_PRELOAD=str(self.shim),
                   MOZART_FAULT_READ='after_first_again', MOZART_FAULT_WRITE='device_gone')
        process = self.start([BINARY, '-d', 'null', '-o', 'null', '--no-rnnoise',
                              '-p', str(listener.getsockname()[1])], env)
        _, error = process.communicate(timeout=2)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b'status=failed stage=write', error)
        self.assertNotIn(b'status=failed stage=capture', error)
        self.assertNotIn(b'[fault] blocking_open', error)

    def test_capture_read_failure_returns_nonzero(self):
        process = self.fault_process('read', 'device_gone')
        _, error = process.communicate(timeout=2)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b'status=failed stage=capture', error)

    def test_alsa_cancellation_without_stop_request_is_failure(self):
        for direction in ['read', 'write']:
            with self.subTest(direction=direction):
                process = self.fault_process(direction, 'cancelled')
                _, error = process.communicate(timeout=2)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(f'code={-errno.ECANCELED}'.encode(), error)

    def test_stalled_io_and_recovery_have_deadline(self):
        for direction in ['read', 'write']:
            for mode in ['again', 'suspend', 'xrun']:
                with self.subTest(direction=direction, mode=mode):
                    process = self.fault_process(direction, mode)
                    _, error = process.communicate(timeout=3)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertIn(f'code={-errno.ETIMEDOUT}'.encode(), error)
                    self.assertIn(f'[fault] {direction}'.encode(), error)

    def test_signal_cancels_stalled_io_and_recovery(self):
        for direction in ['read', 'write']:
            for mode in ['again', 'suspend', 'xrun']:
                with self.subTest(direction=direction, mode=mode):
                    process = self.fault_process(direction, mode, finite=False)
                    marker = b'[fault] resume' if mode == 'suspend' else f'[fault] {direction}'.encode()
                    before = self.wait_marker(process, marker)
                    start = time.monotonic()
                    process.send_signal(signal.SIGTERM)
                    _, error = process.communicate(timeout=.75)
                    self.assertEqual(process.returncode, 0, (before+error).decode())
                    self.assertLess(time.monotonic()-start, .75)
                    self.assertNotIn(b'status=failed', before+error)
                    self.assertNotIn(b'[fault] blocking_open', before+error)

    def test_recoverable_errors_and_partial_io_complete(self):
        for direction in ['read', 'write']:
            for mode in ['recover_suspend', 'recover_xrun', 'partial']:
                with self.subTest(direction=direction, mode=mode):
                    process = self.fault_process(direction, mode)
                    _, error = process.communicate(timeout=2)
                    self.assertEqual(process.returncode, 0, error.decode())
                    self.assertIn(f'[fault] {direction}'.encode(), error)
                    if mode == 'recover_suspend':
                        self.assertIn(b'[fault] resume', error)
                    self.assertNotIn(b'status=failed', error)
                    self.assertNotIn(b'[fault] blocking_open', error)

    def test_missing_backend_output_fails_finite_drain(self):
        listener = self.listener()
        process = self.start(self.command(listener, '--pace', '-o', 'null', '-n', '1'))
        listener.recv(4096)
        _, error = process.communicate(timeout=3)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b'status=failed stage=drain', error)


if __name__ == '__main__':
    unittest.main()
