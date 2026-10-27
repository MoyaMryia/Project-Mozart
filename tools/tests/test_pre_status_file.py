"""Build mozart-pre from source and check --status-file with file replay."""
from pathlib import Path
import json
import os
import re
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import time
import unittest
import wave

REPO = Path(__file__).resolve().parents[2]
BUILD = Path(os.environ.get('MOZART_PRE_STATUS_BUILD', REPO/'build'/'pre-status-check'))
BINARY = BUILD/'mozart-pre'
TELEMETRY_KEYS = {
    'pid', 'state', 'reason', 'mode', 'stages', 'frames', 'direct_periods',
    'skipped_replies', 'send_errors', 'direct_us', 'ns_gain', 'gate_open',
    'vad_conf',
}


def configure_build():
    """Configure once; tests reuse the incremental build directory."""
    command = ['cmake', '-S', str(REPO/'preprocessor'), '-B', str(BUILD),
               '-DCMAKE_BUILD_TYPE=Debug', '-DMOZART_USE_RNNOISE=OFF']
    if not (BUILD/'CMakeCache.txt').exists() and shutil.which('ninja'):
        command += ['-G', 'Ninja']
    subprocess.run(command, check=True, capture_output=True, timeout=120)
    subprocess.run(['cmake', '--build', str(BUILD), '--target', 'mozart-pre',
                    '-j', str(min(os.cpu_count() or 1, 4))],
                   check=True, capture_output=True, timeout=180)


def make_wav(path, frames=960):
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(struct.pack('<%dh' % (2 * frames), *([1000] * (2 * frames))))


def run_pre(args, timeout=10):
    process = subprocess.Popen([str(BINARY), *map(str, args)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        out, err = process.communicate()
        raise AssertionError('mozart-pre timed out: ' + err.decode(errors='replace'))
    return process.pid, process.returncode, out, err


class StatusFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configure_build()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mozart-pre-status-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wav = self.root/'input.wav'
        make_wav(self.wav)
        self.status = self.root/'status.json'

    def listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1', 0))
        sock.settimeout(3)
        self.addCleanup(sock.close)
        return sock

    def base_args(self, listener, status=None, frames=25):
        args = ['-i', self.wav, '--no-denoise', '--ctl-port', '0',
                '-a', '127.0.0.1', '-p', listener.getsockname()[1], '-n', frames]
        if status is not None:
            args += ['--status-file', status]
        return args

    def read_status(self):
        return json.loads(self.status.read_text())

    def test_replay_records_pid_and_frames(self):
        listener = self.listener()
        pid, code, _, err = run_pre(self.base_args(listener, self.status))
        self.assertEqual(code, 0, err.decode())
        status = self.read_status()
        self.assertEqual(set(status), TELEMETRY_KEYS)
        self.assertEqual(status['pid'], pid)
        self.assertEqual(status['state'], 'stopped')
        self.assertEqual(status['reason'], '')
        self.assertEqual(status['frames'], 25)
        self.assertEqual(status['mode'], 'ai')
        self.assertEqual(status['send_errors'], 0)
        self.assertIsInstance(status['direct_us'], dict)

    def test_status_ready_then_stopped_on_signal(self):
        listener = self.listener()
        args = ['-i', self.wav, '--no-denoise', '--ctl-port', '0',
                '-a', '127.0.0.1', '-p', listener.getsockname()[1],
                '--status-file', self.status, '--pace']
        process = subprocess.Popen([str(BINARY), *map(str, args)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            state = ''
            while time.monotonic() < deadline:
                try:
                    state = json.loads(self.status.read_text()).get('state', '')
                except (FileNotFoundError, json.JSONDecodeError):
                    state = ''
                if state == 'ready':
                    break
                time.sleep(.05)
            self.assertEqual(state, 'ready')
            process.send_signal(signal.SIGTERM)
            _, err = process.communicate(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 0, err.decode())
        status = self.read_status()
        self.assertEqual(status['state'], 'stopped')
        self.assertEqual(status['pid'], process.pid)

    def test_bad_wav_records_failed_reason(self):
        bad = self.root/'bad.wav'
        bad.write_bytes(b'not a wav')
        pid, code, _, err = run_pre(['-i', bad, '--no-denoise', '--ctl-port', '0',
                                     '-p', '18999', '--status-file', self.status, '-n', '2'])
        self.assertNotEqual(code, 0)
        self.assertIn(b'stage=wav_open', err)
        status = self.read_status()
        self.assertEqual(status['pid'], pid)
        self.assertEqual(status['state'], 'failed')
        self.assertEqual(status['reason'], 'wav_open')
        self.assertEqual(status['frames'], 0)

    def test_status_write_failure_stops_and_reports(self):
        missing = self.root/'missing'/'status.json'
        listener = self.listener()
        _, code, _, err = run_pre([*self.base_args(listener, missing, 100), '--pace'])
        self.assertNotEqual(code, 0)
        self.assertIn(b'status=failed stage=status_file', err)
        sent = re.search(r'结束: 发送 (\d+)'.encode(), err)
        self.assertIsNotNone(sent)
        self.assertLess(int(sent.group(1)), 100)
        self.assertFalse(missing.exists())

    def test_control_port_in_use_fails_and_records_reason(self):
        listener = self.listener()
        occupied = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        occupied.bind(('127.0.0.1', 0))
        self.addCleanup(occupied.close)
        args = ['-i', self.wav, '--no-denoise', '--ctl-port', occupied.getsockname()[1],
                '--status-file', self.status, '-a', '127.0.0.1',
                '-p', listener.getsockname()[1], '-n', '2']
        _, code, _, err = run_pre(args)
        self.assertNotEqual(code, 0)
        self.assertIn(b'[ctl] bind', err)
        self.assertIn(b'stage=control_open', err)
        status = self.read_status()
        self.assertEqual(status['state'], 'failed')
        self.assertEqual(status['reason'], 'control_open')

    def test_status_file_rejects_input_inode(self):
        before = self.wav.read_bytes()
        alias = self.root/'alias.json'
        os.link(self.wav, alias)
        _, code, _, err = run_pre(['-i', self.wav, '--no-denoise', '--ctl-port', '0',
                                   '--status-file', alias, '-p', '18999', '-n', '2'])
        self.assertNotEqual(code, 0)
        self.assertIn(b'reason=status_file aliases input', err)
        self.assertEqual(self.wav.read_bytes(), before)
        self.assertEqual(alias.read_bytes(), before)

    def test_dual_send_packets_match_without_status_file(self):
        first, second = self.listener(), self.listener()
        process = subprocess.Popen(
            [str(BINARY), *map(str, self.base_args(first, None, 12)),
             '-b', f'127.0.0.1:{second.getsockname()[1]}'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        packets = {first: [], second: []}
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(first, selectors.EVENT_READ)
                selector.register(second, selectors.EVENT_READ)
                deadline = time.monotonic() + 5
                while min(map(len, packets.values())) < 12 and time.monotonic() < deadline:
                    for key, _ in selector.select(.1):
                        packets[key.fileobj].append(key.fileobj.recv(4096))
            _, err = process.communicate(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 0, err.decode())
        self.assertEqual(len(packets[first]), 12)
        self.assertEqual(packets[first], packets[second])
        self.assertFalse(self.status.exists())


if __name__ == '__main__':
    unittest.main()
