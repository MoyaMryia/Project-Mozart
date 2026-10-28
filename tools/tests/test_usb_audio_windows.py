"""Windows 桥接程序：真实帧缓冲、双工停机与退出码的回归测试。"""
import array
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import wave

USB_ROOT = Path(__file__).resolve().parents[2] / 'usb-audio'
sys.path.insert(0, str(USB_ROOT))
import proto
import usb.core


def load_bridge():
    spec = importlib.util.spec_from_file_location('windows_bridge',
                                                  USB_ROOT / 'windows-bridge.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = load_bridge()

FRAME = bytes(proto.FRAME_BYTES)
FAST = bytes(proto.FAST_BYTES)


class BufferEndpoint:
    """bulk 端点语义：read 返回 array('B')，长度不超过请求值。"""

    def __init__(self, data=b'', stopping=None, error=None, chunk=None, timeouts=0):
        self.data = bytes(data)
        self.stopping = stopping
        self.error = error
        self.chunk = chunk
        self.timeouts = timeouts

    def read(self, size, timeout=0):
        if self.timeouts:
            self.timeouts -= 1
            raise usb.core.USBTimeoutError('read timeout')
        if self.data:
            limit = size if self.chunk is None else min(size, self.chunk)
            piece, self.data = self.data[:limit], self.data[limit:]
            return array.array('B', piece)
        if self.error is not None:
            raise self.error
        if self.stopping is not None:
            self.stopping.set()
        return array.array('B')


class FakeSink:
    def __init__(self):
        self.payloads = []
        self.frames = 0

    def write(self, payload):
        self.payloads.append(payload)
        self.frames += 1


class FailingSink:
    def __init__(self):
        self.frames = 0

    def write(self, _payload):
        raise bridge.AudioError('写播放设备失败：device lost')


class SessionTests(unittest.TestCase):
    def test_first_error_is_queryable_and_requests_stop(self):
        session = bridge.Session()
        session.fail('读 USB 失败：disconnected')
        self.assertTrue(session.stopping.is_set())
        self.assertEqual(session.error, '读 USB 失败：disconnected')
        self.assertEqual(session.exit_code, 1)
        session.fail('第二个错误')
        self.assertEqual(session.error, '读 USB 失败：disconnected')


class ReceiveTests(unittest.TestCase):
    def test_accepts_both_frame_sizes(self):
        stopping = threading.Event()
        fast = b'\x01\x02' * proto.FAST_SAMPLES * proto.CHANNELS
        legacy = b'\x03\x04' * (proto.RATE * proto.FRAME_MS // 1000) * proto.CHANNELS
        stream = (
            proto.encode(proto.TYPE_TELEMETRY, 0, 0, b'telemetry')
            + proto.encode(proto.TYPE_PCM, 1, 0, fast, stream=proto.STREAM_FAST)
            + proto.encode(proto.TYPE_PCM, 2, 0, legacy, stream=proto.STREAM_AUDIO)
            + proto.encode(proto.TYPE_PCM, 3, 0, b'control', stream=proto.STREAM_CONTROL)
        )
        sink = FakeSink()
        with mock.patch('sys.stderr'):
            code = bridge.receive(BufferEndpoint(stream, stopping), sink, stopping,
                                  show_stats=False)
        self.assertEqual(code, 0)
        self.assertEqual(sink.payloads, [fast, legacy])

    def test_audio_with_unexpected_length_is_skipped(self):
        stopping = threading.Event()
        sink = FakeSink()
        frame = proto.encode(proto.TYPE_PCM, 1, 0, b'x' * 100, stream=proto.STREAM_AUDIO)
        with mock.patch('sys.stderr'):
            code = bridge.receive(BufferEndpoint(frame, stopping), sink, stopping,
                                  show_stats=False)
        self.assertEqual(code, 0)
        self.assertEqual(sink.payloads, [])

    def test_partial_reads_are_buffered(self):
        stopping = threading.Event()
        payload = b'\x05\x06' * (proto.RATE * proto.FRAME_MS // 1000) * proto.CHANNELS
        frame = proto.encode(proto.TYPE_PCM, 7, 0, payload, stream=proto.STREAM_AUDIO)
        sink = FakeSink()
        code = bridge.receive(BufferEndpoint(frame, stopping, chunk=13), sink, stopping,
                              show_stats=False)
        self.assertEqual(code, 0)
        self.assertEqual(sink.payloads, [payload])

    def test_read_timeout_keeps_receiving(self):
        stopping = threading.Event()
        payload = b'\x07\x08' * (proto.RATE * proto.FRAME_MS // 1000) * proto.CHANNELS
        frame = proto.encode(proto.TYPE_PCM, 1, 0, payload, stream=proto.STREAM_AUDIO)
        sink = FakeSink()
        code = bridge.receive(BufferEndpoint(frame, stopping, timeouts=1), sink, stopping,
                              show_stats=False)
        self.assertEqual(code, 0)
        self.assertEqual(sink.payloads, [payload])

    def test_usb_error_reports_and_returns_nonzero(self):
        stopping = threading.Event()
        errors = []
        endpoint = BufferEndpoint(error=usb.core.USBError('device disconnected'))
        with mock.patch('sys.stderr'):
            code = bridge.receive(endpoint, FakeSink(), stopping, show_stats=False,
                                  on_error=errors.append)
        self.assertEqual(code, 1)
        self.assertTrue(stopping.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIn('读 USB 失败', errors[0])

    def test_sink_error_reports_and_returns_nonzero(self):
        stopping = threading.Event()
        errors = []
        frame = proto.encode(proto.TYPE_PCM, 1, 0, FRAME, stream=proto.STREAM_AUDIO)
        with mock.patch('sys.stderr'):
            code = bridge.receive(BufferEndpoint(frame, stopping), FailingSink(), stopping,
                                  show_stats=False, on_error=errors.append)
        self.assertEqual(code, 1)
        self.assertTrue(stopping.is_set())
        self.assertIn('写播放设备失败', errors[0])


class ScriptedInputStream:
    """按脚本在独立线程调用回调，模拟 PortAudio 采集线程。"""

    def __init__(self, script):
        self.script = script
        self.streams = []

    def __call__(self, **kwargs):
        stream = FakeInputStream(self.script, kwargs['callback'], kwargs['finished_callback'])
        self.streams.append(stream)
        return stream


class FakeInputStream:
    def __init__(self, script, callback, finished_callback):
        self.script = script
        self.callback = callback
        self.finished_callback = finished_callback
        self.closing = threading.Event()
        self.exited = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def _run(self):
        try:
            self.script(self.callback, self.closing)
        finally:
            self.finished_callback()

    def __exit__(self, *info):
        self.closing.set()
        self.thread.join(timeout=5)
        self.exited.set()
        return False


class FakePortAudioError(Exception):
    pass


class FailingInput:
    def __init__(self, error=None):
        self.error = error or FakePortAudioError('no input device')

    def __call__(self, **_kwargs):
        raise self.error


def make_sounddevice(input_script=None, output_factory=None):
    module = types.ModuleType('sounddevice')
    module.PortAudioError = FakePortAudioError
    module.RawInputStream = (ScriptedInputStream(input_script) if input_script
                             else FailingInput())
    module.RawOutputStream = output_factory if output_factory else FailingInput()
    return module


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class CaptureTests(unittest.TestCase):
    def test_capture_sends_complete_bytes_from_worker_thread(self):
        stopping = threading.Event()
        written = []
        write_threads = []
        write_timeouts = []
        callback_threads = []
        samples = bytes(range(256)) * 30

        class Out:
            def write(self, data, timeout=0):
                write_timeouts.append(timeout)
                written.append(bytes(data))
                write_threads.append(threading.current_thread())
                return len(data)

        def script(callback, closing):
            callback_threads.append(threading.current_thread())
            callback(memoryview(samples[:4096]), 1024, None, None)
            callback(memoryview(samples[4096:]), 896, None, None)
            closing.wait(5)

        errors = []
        fake_sd = make_sounddevice(input_script=script)
        result = {}

        def run():
            result['code'] = bridge.capture(Out(), stopping, on_error=errors.append)

        stderr = io.StringIO()
        thread = threading.Thread(target=run)
        with mock.patch('sys.stderr', stderr), \
                mock.patch.dict(sys.modules, {'sounddevice': fake_sd}):
            thread.start()
            self.assertTrue(wait_until(lambda: len(written) == 2))
            stopping.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(result['code'], 0)
        self.assertEqual(errors, [])
        self.assertEqual(write_timeouts, [2000, 2000])
        self.assertEqual(len(written), 2)
        frames = [proto.decode(data) for data in written]
        self.assertEqual(b''.join(frame[-1] for frame in frames), samples)
        for sequence, frame in enumerate(frames):
            version, kind, stream, _flags, seq, _ts, payload = frame
            self.assertEqual(version, proto.VERSION)
            self.assertEqual((kind, stream, seq), (proto.TYPE_PCM, proto.STREAM_AUDIO, sequence))
            self.assertEqual(len(payload), proto.FRAME_BYTES)
        self.assertTrue(all(t is not callback_threads[0] for t in write_threads))

    def test_write_timeout_does_not_block_callback_and_drops_are_counted(self):
        stopping = threading.Event()
        depth = bridge.CAPTURE_QUEUE_BLOCKS
        durations = []

        class BlockingOut:
            def __init__(self):
                self.started = threading.Event()
                self.release = threading.Event()
                self.calls = 0
                self.timeouts = []

            def write(self, _data, timeout=0):
                self.calls += 1
                self.timeouts.append(timeout)
                self.started.set()
                self.release.wait(5)
                raise usb.core.USBTimeoutError('write timeout')

        out = BlockingOut()

        def script(callback, closing):
            callback(FRAME, None, None, None)
            self.assertTrue(out.started.wait(2))
            for _ in range(depth + 3):
                start = time.monotonic()
                callback(FRAME, None, None, None)
                durations.append(time.monotonic() - start)
            out.release.set()
            closing.wait(2)

        fake_sd = make_sounddevice(input_script=script)
        errors = []
        stderr = io.StringIO()
        with mock.patch('sys.stderr', stderr), \
                mock.patch.dict(sys.modules, {'sounddevice': fake_sd}):
            code = bridge.capture(out, stopping, on_error=errors.append)
        self.assertEqual(code, 1)
        self.assertTrue(stopping.is_set())
        self.assertIn('写 USB 失败', errors[0])
        self.assertEqual(out.calls, 1)
        self.assertEqual(out.timeouts, [2000])
        self.assertLess(max(durations), 0.2)
        self.assertIn('共丢弃 3 帧', stderr.getvalue())

    def test_input_stream_unexpected_finish_returns_nonzero(self):
        stopping = threading.Event()
        errors = []
        fake_sd = make_sounddevice(input_script=lambda _callback, _closing: None)
        with mock.patch('sys.stderr'), mock.patch.dict(sys.modules, {'sounddevice': fake_sd}):
            code = bridge.capture(mock.Mock(), stopping, on_error=errors.append)
        self.assertEqual(code, 1)
        self.assertTrue(stopping.is_set())
        self.assertEqual(errors, ['音频采集流意外结束'])

    def test_input_stream_failure_returns_nonzero(self):
        stopping = threading.Event()
        errors = []
        fake_sd = make_sounddevice()
        with mock.patch('sys.stderr'), \
                mock.patch.dict(sys.modules, {'sounddevice': fake_sd}):
            code = bridge.capture(lambda *_args, **_kwargs: 0, stopping,
                                  on_error=errors.append)
        self.assertEqual(code, 1)
        self.assertTrue(stopping.is_set())
        self.assertIn('音频采集失败', errors[0])


class SinkTests(unittest.TestCase):
    def test_stream_close_runs_even_if_wav_close_fails(self):
        output = mock.Mock()
        fake_sd = make_sounddevice(output_factory=lambda **_kwargs: output)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(sys.modules, {'sounddevice': fake_sd}):
            sink = bridge.Sink(Path(directory)/'rx.wav', play=True)
            writer = sink.writer
            close = writer.close
            with mock.patch.object(writer, 'close', side_effect=OSError('disk full')), \
                    self.assertRaises(OSError):
                sink.close()
            output.close.assert_called_once()
            close()

    def test_stream_close_runs_even_if_stop_fails(self):
        output = mock.Mock()
        output.stop.side_effect = FakePortAudioError('stop failed')
        fake_sd = make_sounddevice(output_factory=lambda **_kwargs: output)
        with mock.patch.dict(sys.modules, {'sounddevice': fake_sd}):
            sink = bridge.Sink(play=True)
            with self.assertRaises(bridge.AudioError):
                sink.close()
        output.close.assert_called_once()

    def test_play_open_failure_closes_wav_and_raises_audio_error(self):
        class BrokenOutput:
            def __init__(self, **_kwargs):
                raise FakePortAudioError('no output device')

        fake_sd = make_sounddevice(output_factory=BrokenOutput)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rx.wav'
            with mock.patch.dict(sys.modules, {'sounddevice': fake_sd}):
                with self.assertRaises(bridge.AudioError):
                    bridge.Sink(path, play=True)
            with wave.open(str(path), 'rb') as reader:
                self.assertEqual(reader.getnframes(), 0)

    def test_play_write_failure_raises_audio_error(self):
        class BrokenOutput:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                pass

            def write(self, _payload):
                raise FakePortAudioError('write failed')

            def stop(self):
                pass

            def close(self):
                pass

        fake_sd = make_sounddevice(output_factory=BrokenOutput)
        with mock.patch.dict(sys.modules, {'sounddevice': fake_sd}):
            sink = bridge.Sink(play=True)
            with self.assertRaises(bridge.AudioError):
                sink.write(FRAME)
            sink.close()


def run_main(arguments, out=None, inp=None, sounddevice=None, sink_class=None):
    stderr = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(sys, 'argv', ['windows-bridge.py'] + arguments))
        stack.enter_context(mock.patch.object(bridge, 'open_device',
                                              return_value=(None, out, inp)))
        stack.enter_context(mock.patch('sys.stderr', stderr))
        if sink_class is not None:
            stack.enter_context(mock.patch.object(bridge, 'Sink', sink_class))
        if sounddevice is not None:
            stack.enter_context(mock.patch.dict(sys.modules, {'sounddevice': sounddevice}))
        code = bridge.main()
    return code, stderr.getvalue()


class MainTests(unittest.TestCase):
    def test_unexpected_receive_error_stops_duplex_and_returns_nonzero(self):
        endpoint = types.SimpleNamespace(read=mock.Mock(side_effect=ValueError('invalid transfer')))
        input_factory = ScriptedInputStream(lambda _callback, closing: closing.wait(5))
        fake_sd = make_sounddevice()
        fake_sd.RawInputStream = input_factory
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(threading, 'excepthook') as hook:
            start = time.monotonic()
            code, stderr = run_main(['--record', str(Path(directory)/'rx.wav'),
                                     '--capture', '--seconds', '5'],
                                    inp=endpoint, out=mock.Mock(), sounddevice=fake_sd)
            self.assertEqual(code, 1)
            self.assertLess(time.monotonic()-start, 2)
            self.assertIn('invalid transfer', stderr)
            hook.assert_called_once()
            self.assertTrue(input_factory.streams[0].exited.is_set())

    def test_unwritable_record_path_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            code, stderr = run_main(['--record', str(Path(directory)/'missing'/'rx.wav')])
        self.assertEqual(code, 1)
        self.assertIn('打开 WAV 失败', stderr)

    def test_invalid_seconds_is_rejected_before_opening_devices(self):
        for seconds in ('-1', 'nan', 'inf'):
            with self.subTest(seconds=seconds), mock.patch.object(bridge, 'open_device') as opened, \
                    mock.patch.object(sys, 'argv', ['windows-bridge.py', '--capture', '--seconds', seconds]), \
                    mock.patch('sys.stderr'), self.assertRaises(SystemExit) as raised:
                bridge.main()
            self.assertEqual(raised.exception.code, 2)
            opened.assert_not_called()

    def test_successful_record_returns_zero_and_closes_wav(self):
        stopping = threading.Event()
        first = proto.encode(proto.TYPE_PCM, 1, 0, FRAME, stream=proto.STREAM_AUDIO)
        second = proto.encode(proto.TYPE_PCM, 2, 0, FRAME, stream=proto.STREAM_AUDIO)
        endpoint = BufferEndpoint(first + second, stopping,
                                  error=usb.core.USBTimeoutError('idle'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rx.wav'
            code, _ = run_main(['--record', str(path), '--seconds', '0.3'], inp=endpoint)
            self.assertEqual(code, 0)
            with wave.open(str(path), 'rb') as reader:
                self.assertEqual(reader.getnframes(), 2 * proto.RATE * proto.FRAME_MS // 1000)
                self.assertEqual(reader.getnchannels(), proto.CHANNELS)
                self.assertEqual(reader.getframerate(), proto.RATE)

    def test_timed_wait_returns_early_on_usb_failure(self):
        endpoint = BufferEndpoint(error=usb.core.USBError('device disconnected'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rx.wav'
            start = time.monotonic()
            code, stderr = run_main(['--record', str(path), '--seconds', '5'], inp=endpoint)
            elapsed = time.monotonic() - start
            self.assertEqual(code, 1)
            self.assertLess(elapsed, 2)
            self.assertIn('读 USB 失败', stderr)

    def test_timed_wait_returns_early_on_usb_write_failure(self):
        class BrokenOut:
            def write(self, _data, timeout=0):
                raise usb.core.USBError('write failed')

        def script(callback, closing):
            callback(FRAME, None, None, None)
            closing.wait(5)

        fake_sd = make_sounddevice(input_script=script)
        start = time.monotonic()
        code, stderr = run_main(['--capture', '--seconds', '5'], out=BrokenOut(),
                                sounddevice=fake_sd)
        elapsed = time.monotonic() - start
        self.assertEqual(code, 1)
        self.assertLess(elapsed, 2)
        self.assertIn('写 USB 失败', stderr)

    def test_duplex_usb_disconnect_stops_capture_before_sink_close(self):
        endpoint = BufferEndpoint(error=usb.core.USBError('device disconnected'))
        input_factory = ScriptedInputStream(lambda _callback, closing: closing.wait(5))
        created = []

        class Out:
            def __init__(self):
                self.closed = False
                self.closed_after_capture = None

            def start(self):
                pass

            def write(self, _payload):
                pass

            def stop(self):
                pass

            def close(self):
                self.closed = True
                streams = input_factory.streams
                self.closed_after_capture = bool(streams) and streams[0].exited.is_set()

        def output_factory(**_kwargs):
            stream = Out()
            created.append(stream)
            return stream

        fake_sd = make_sounddevice(output_factory=output_factory)
        fake_sd.RawInputStream = input_factory
        code, stderr = run_main(['--play', '--capture', '--seconds', '5'],
                                out=mock.Mock(write=mock.Mock()), inp=endpoint,
                                sounddevice=fake_sd)
        self.assertEqual(code, 1)
        self.assertIn('读 USB 失败', stderr)
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].closed)
        self.assertTrue(created[0].closed_after_capture)

    def test_sink_close_waits_for_receive_thread(self):
        stopping = threading.Event()
        frames = b''.join(proto.encode(proto.TYPE_PCM, seq, 0, FRAME,
                                       stream=proto.STREAM_AUDIO) for seq in range(50))
        endpoint = BufferEndpoint(frames, stopping)
        endpoint.error = usb.core.USBTimeoutError('idle')
        created = []

        class SlowWaveSink(bridge.Sink):
            def __init__(self, path=None, play=False):
                super().__init__(path, play)
                self.closed = False
                self.writes_after_close = 0
                created.append(self)

            def write(self, payload):
                time.sleep(0.02)
                if self.closed:
                    self.writes_after_close += 1
                super().write(payload)

            def close(self):
                self.closed = True
                super().close()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rx.wav'
            code, _ = run_main(['--record', str(path), '--seconds', '0.05'], inp=endpoint,
                               sink_class=SlowWaveSink)
            self.assertEqual(code, 0)
            self.assertEqual(len(created), 1)
            self.assertTrue(created[0].closed)
            self.assertGreater(created[0].frames, 0)
            self.assertEqual(created[0].writes_after_close, 0)
            with wave.open(str(path), 'rb') as reader:
                self.assertEqual(reader.getnframes(),
                                 created[0].frames * proto.RATE * proto.FRAME_MS // 1000)


if __name__ == '__main__':
    unittest.main()
