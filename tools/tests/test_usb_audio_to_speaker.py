"""USB 转扬声器脚本：帧缓冲、入口校验与故障退出码的回归测试。"""
import array
import contextlib
import importlib.util
import io
import os
import pathlib
import subprocess
import sys
import threading
import unittest
from unittest import mock

USB_ROOT = pathlib.Path(__file__).resolve().parents[2] / 'usb-audio'
sys.path.insert(0, str(USB_ROOT))
import proto
import usb.core


def load_module():
    spec = importlib.util.spec_from_file_location('usb_to_speaker',
                                                  USB_ROOT / 'usb-to-speaker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


speaker = load_module()

FAST_FRAME = proto.encode(proto.TYPE_PCM, 0, 0, bytes(proto.FAST_BYTES),
                          stream=proto.STREAM_FAST)
AUDIO_FRAME = proto.encode(proto.TYPE_PCM, 1, 0, bytes(proto.FRAME_BYTES),
                           stream=proto.STREAM_AUDIO)


class BufferEndpoint:
    """bulk IN 端点语义：read 返回 array('B')；数据用完后按超时处理。"""

    def __init__(self, data=b'', chunk=None, error=None, pieces=None):
        self.data = bytes(data)
        self.pieces = list(pieces) if pieces is not None else []
        self.chunk = chunk
        self.error = error

    def read(self, size, timeout=0):
        if not self.data and self.pieces:
            self.data = self.pieces.pop(0)
        if self.data:
            limit = size if self.chunk is None else min(size, self.chunk)
            piece, self.data = self.data[:limit], self.data[limit:]
            return array.array('B', piece)
        if self.error is not None:
            raise self.error
        raise usb.core.USBTimeoutError('idle')


class FakeSink:
    def __init__(self):
        self.payloads = []

    def write(self, payload):
        self.payloads.append(payload)


class FailingSink:
    def write(self, payload):
        raise speaker.AudioError('设备忙')


def run_receive(endpoint, sink, seconds=0.2):
    stopping = threading.Event()
    counters = speaker.Counters()
    timer = threading.Timer(seconds, stopping.set)
    timer.start()
    try:
        code = speaker.receive(endpoint, sink, stopping, counters)
    finally:
        timer.cancel()
    return code, counters


class ReceiveTests(unittest.TestCase):
    def test_both_audio_frame_sizes_are_played(self):
        code, counters = run_receive(BufferEndpoint(FAST_FRAME + AUDIO_FRAME), FakeSink())
        self.assertEqual(code, 0)
        self.assertEqual(counters.frames, 2)
        self.assertEqual(counters.rejected, 0)
        self.assertEqual(counters.gaps, 0)

    def test_payloads_reach_the_sink_in_order(self):
        sink = FakeSink()
        run_receive(BufferEndpoint(FAST_FRAME + AUDIO_FRAME), sink)
        self.assertEqual([len(payload) for payload in sink.payloads],
                         [proto.FAST_BYTES, proto.FRAME_BYTES])

    def test_partial_reads_are_buffered(self):
        code, counters = run_receive(BufferEndpoint(AUDIO_FRAME, chunk=97), FakeSink())
        self.assertEqual(code, 0)
        self.assertEqual(counters.frames, 1)

    def test_wrong_version_is_dropped(self):
        bad = bytearray(FAST_FRAME)
        bad[2] = proto.VERSION + 1
        with contextlib.redirect_stderr(io.StringIO()):
            code, counters = run_receive(BufferEndpoint(bytes(bad)), FakeSink())
        self.assertEqual(code, 0)
        self.assertEqual((counters.frames, counters.rejected), (0, 1))

    def test_control_and_unexpected_length_are_dropped(self):
        control = proto.control(proto.CONTROL_PING)
        short = proto.encode(proto.TYPE_PCM, 2, 0, bytes(100))
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            code, counters = run_receive(BufferEndpoint(control + short), FakeSink())
        self.assertEqual(code, 0)
        self.assertEqual(counters.frames, 0)
        self.assertEqual(counters.rejected, 2)
        self.assertIn('非音频帧或帧长非法', stderr.getvalue())

    def test_sequence_gap_is_counted(self):
        frames = proto.encode(proto.TYPE_PCM, 5, 0, bytes(proto.FAST_BYTES),
                              stream=proto.STREAM_FAST)
        frames += proto.encode(proto.TYPE_PCM, 9, 0, bytes(proto.FAST_BYTES),
                               stream=proto.STREAM_FAST)
        code, counters = run_receive(BufferEndpoint(frames), FakeSink())
        self.assertEqual(code, 0)
        self.assertEqual(counters.gaps, 1)

    def test_header_error_discards_the_whole_buffer(self):
        # 同一块缓冲里的有效帧随损坏帧一起丢弃，下一块缓冲照常解帧。
        broken = bytearray(FAST_FRAME)
        broken[0] ^= 0xff
        pieces = [bytes(broken) + FAST_FRAME, FAST_FRAME]
        with contextlib.redirect_stderr(io.StringIO()):
            code, counters = run_receive(BufferEndpoint(pieces=pieces), FakeSink())
        self.assertEqual(code, 0)
        self.assertEqual(counters.frames, 1)

    def test_usb_error_returns_nonzero(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code, _counters = run_receive(
                BufferEndpoint(error=usb.core.USBError('管道断开')), FakeSink())
        self.assertEqual(code, 1)

    def test_sink_error_returns_nonzero(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code, counters = run_receive(BufferEndpoint(FAST_FRAME), FailingSink())
        self.assertEqual(code, 1)
        self.assertEqual(counters.frames, 1)


class MetricsTests(unittest.TestCase):
    def test_silence_reports_the_floor(self):
        self.assertEqual(speaker.rms_dbfs(bytes(proto.FAST_BYTES)), speaker.SILENT_DBFS)

    def test_full_scale_reports_zero(self):
        payload = b'\xff\x7f' * (proto.FAST_BYTES // 2)
        self.assertAlmostEqual(speaker.rms_dbfs(payload), 0.0, places=1)


class ServiceTests(unittest.TestCase):
    def check(self, state):
        recorded = []

        def fake_run(argv, **kwargs):
            recorded.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=state + '\n', stderr='')

        with mock.patch.object(subprocess, 'run', fake_run):
            speaker.check_service()
        return recorded

    def test_active_service_is_refused(self):
        with self.assertRaises(SystemExit):
            self.check('active')

    def test_restart_window_is_refused(self):
        for state in ('activating', 'deactivating'):
            with self.assertRaises(SystemExit):
                self.check(state)

    def test_stopped_service_passes(self):
        recorded = self.check('inactive')
        self.assertEqual(recorded, [['systemctl', 'is-active', speaker.SERVICE]])

    def test_missing_systemctl_is_reported(self):
        def missing(argv, **kwargs):
            raise FileNotFoundError('systemctl')

        with mock.patch.object(subprocess, 'run', missing), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            speaker.check_service()
        self.assertIn('跳过服务占用检查', stderr.getvalue())


class FakeProcess:
    """aplay 进程替身：stdin 使用真实管道，便于测试写入路径。"""

    def __init__(self, argv, stdin=None, stderr=None):
        self.argv = list(argv)
        self.exited = None
        self.waits = 0
        self.terminated = False
        read_end, write_end = os.pipe()
        os.close(read_end)
        self.stdin = os.fdopen(write_end, 'wb')
        FakeProcess.created.append(self)

    created = []

    def poll(self):
        return self.exited

    def wait(self, timeout=None):
        self.waits += 1
        if self.terminated:
            return 0
        raise subprocess.TimeoutExpired('aplay', timeout)

    def terminate(self):
        self.terminated = True


class PlayerTests(unittest.TestCase):
    def make_player(self, stopping=None):
        FakeProcess.created = []
        with mock.patch.object(speaker.subprocess, 'Popen', FakeProcess), \
                mock.patch.object(speaker.os, 'set_blocking'), \
                mock.patch.object(speaker.fcntl, 'fcntl'):
            return speaker.Player('default', stopping or threading.Event())

    def test_aplay_arguments_carry_the_requested_device(self):
        player = self.make_player()
        player.close()
        argv = FakeProcess.created[0].argv
        self.assertEqual(argv[0], 'aplay')
        self.assertIn('default', argv)
        self.assertIn(str(proto.RATE), argv)

    def test_write_failure_becomes_an_audio_error(self):
        player = self.make_player()
        with mock.patch.object(os, 'write', side_effect=OSError(32, '管道断开')):
            with self.assertRaises(speaker.AudioError) as caught:
                player.write(bytes(proto.FAST_BYTES))
        self.assertIn('管道断开', str(caught.exception))
        player.process.exited = 0
        player.close()

    def test_close_terminates_aplay_that_ignores_stdin_close(self):
        player = self.make_player()
        player.close()
        self.assertTrue(player.process.terminated)

    def test_write_is_skipped_after_stop(self):
        stopping = threading.Event()
        stopping.set()
        player = self.make_player(stopping)
        with mock.patch.object(os, 'write') as written:
            player.write(bytes(proto.FAST_BYTES))
        written.assert_not_called()
        player.process.exited = 0
        player.close()


class DeviceTests(unittest.TestCase):
    def test_missing_device_names_the_channel_and_permissions(self):
        with mock.patch.object(usb.core, 'find', return_value=None):
            with self.assertRaises(SystemExit) as caught:
                speaker.open_device()
        message = str(caught.exception)
        self.assertIn('1d6b:0105', message)
        self.assertIn('usbfs', message)

    def test_interface_without_input_endpoint_is_refused(self):
        device = mock.Mock()
        device.get_active_configuration.return_value = {(0, 0): object()}
        with mock.patch.object(usb.core, 'find', return_value=device), \
                mock.patch.object(usb.util, 'find_descriptor', return_value=None):
            with self.assertRaises(SystemExit) as caught:
                speaker.open_device()
        self.assertIn('bulk IN', str(caught.exception))

    def test_missing_interface_zero_is_refused(self):
        device = mock.Mock()
        device.get_active_configuration.return_value = {}
        with mock.patch.object(usb.core, 'find', return_value=device):
            with self.assertRaises(SystemExit) as caught:
                speaker.open_device()
        self.assertIn('接口 0', str(caught.exception))


class FakePlayer:
    def __init__(self, device, stopping):
        self.device = device
        self.stopping = stopping
        self.payloads = []
        self.closed = False

    def write(self, payload):
        self.payloads.append(payload)

    def close(self):
        self.closed = True


class FailingPlayer(FakePlayer):
    def write(self, payload):
        raise speaker.AudioError('设备忙')


class MainTests(unittest.TestCase):
    def run_main(self, endpoint, player_class, seconds='0.2'):
        created = []
        order = []

        def factory(device, stopping):
            player = player_class(device, stopping)
            created.append(player)
            return player

        def check():
            order.append('check')

        def opened():
            order.append('open')
            return None, endpoint

        argv = ['usb-to-speaker.py', '--seconds', seconds, '--device', 'plughw:CARD=Device,DEV=0']
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(speaker.signal, 'signal'), \
                mock.patch.object(speaker, 'check_service', check), \
                mock.patch.object(speaker, 'open_device', opened), \
                mock.patch.object(speaker, 'Player', factory), \
                contextlib.redirect_stdout(io.StringIO()):
            code = speaker.main()
        return code, created, order

    def test_success_returns_zero_and_closes_the_player(self):
        code, created, order = self.run_main(BufferEndpoint(FAST_FRAME), FakePlayer)
        self.assertEqual(code, 0)
        self.assertEqual(order, ['check', 'open'])
        self.assertEqual(created[0].device, 'plughw:CARD=Device,DEV=0')
        self.assertTrue(created[0].closed)
        self.assertEqual(len(created[0].payloads), 1)

    def test_player_failure_returns_nonzero_and_closes_the_player(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code, created, _order = self.run_main(BufferEndpoint(FAST_FRAME), FailingPlayer)
        self.assertEqual(code, 1)
        self.assertTrue(created[0].closed)

    def test_invalid_seconds_is_rejected_before_opening_the_device(self):
        for value in ('-1', 'nan', 'inf'):
            with mock.patch.object(sys, 'argv', ['usb-to-speaker.py', '--seconds', value]), \
                    mock.patch.object(speaker, 'open_device') as opened, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    speaker.main()
            self.assertEqual(caught.exception.code, 2)
            opened.assert_not_called()


if __name__ == '__main__':
    unittest.main()
