"""USB 事件、端点故障退出与管道水位的回归测试。"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import struct
import sys
import types
import unittest
from unittest.mock import Mock, patch

USB_ROOT = Path(__file__).resolve().parents[2] / 'usb-audio'
sys.path.insert(0, str(USB_ROOT))
from pcm import read_block
import proto


def load_daemon(name):
    spec = importlib.util.spec_from_file_location(name, USB_ROOT / f'{name}-daemon.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


jetson = load_daemon('jetson')
host = load_daemon('host')
PAYLOAD = struct.pack('<hh', 1000, 1000) * proto.FAST_SAMPLES


def event(kind):
    return struct.pack('<8sB3x', b'\0' * 8, kind)


class FunctionEventsTests(unittest.TestCase):
    def test_bind_and_enable_in_one_read_start_audio(self):
        daemon = jetson.Daemon()
        with patch.object(daemon, 'enable') as enable:
            daemon.handle_events(event(jetson.EVENT_BIND) + event(jetson.EVENT_ENABLE))
        enable.assert_called_once_with()

    def test_batched_disconnect_and_reconnect_preserve_event_order(self):
        daemon = jetson.Daemon()
        calls = []

        def enable():
            calls.append('enable')
            daemon.active.set()

        def disable():
            calls.append('disable')
            daemon.active.clear()

        with patch.object(daemon, 'enable', side_effect=enable), \
                patch.object(daemon, 'disable', side_effect=disable):
            daemon.handle_events(b''.join(event(kind) for kind in (
                jetson.EVENT_ENABLE, jetson.EVENT_DISABLE, jetson.EVENT_ENABLE)))
        self.assertEqual(calls, ['enable', 'disable', 'enable'])

    def test_event_loop_processes_whole_read(self):
        daemon = jetson.Daemon()
        events = event(jetson.EVENT_BIND) + event(jetson.EVENT_ENABLE) + event(jetson.EVENT_UNBIND)
        with contextlib.ExitStack() as stack:
            for obj, name in ((jetson.signal, 'signal'), (jetson.threading.Thread, 'start'),
                              (jetson.os, 'close'), (daemon, 'start_audio'), (daemon, 'stop_audio')):
                stack.enter_context(patch.object(obj, name))
            stack.enter_context(patch.dict(os.environ, {'MOZART_UDC': ''}))
            stack.enter_context(patch.object(jetson.os, 'open', return_value=10))
            stack.enter_context(patch.object(jetson.os, 'write'))
            read = stack.enter_context(patch.object(jetson.os, 'read', return_value=events))
            stack.enter_context(patch.object(jetson.select, 'select', return_value=([10], [], [])))
            enable = stack.enter_context(patch.object(daemon, 'enable'))
            self.assertEqual(daemon.run(), 0)
        enable.assert_called_once_with()
        read.assert_called_once_with(10, jetson.FFS_EVENT.size * 5)


    def test_event_eof_and_io_errors_request_restart_and_clean_up(self):
        for failure in (b'', OSError('event endpoint lost')):
            with self.subTest(failure=failure), contextlib.ExitStack() as stack:
                daemon = jetson.Daemon()
                for obj, name in ((jetson.signal, 'signal'), (jetson.threading.Thread, 'start'),
                                  (jetson.os, 'close'), (daemon, 'start_audio')):
                    stack.enter_context(patch.object(obj, name))
                stack.enter_context(patch.dict(os.environ, {'MOZART_UDC': ''}))
                stack.enter_context(patch.object(jetson.os, 'open', return_value=10))
                stack.enter_context(patch.object(jetson.os, 'write'))
                stack.enter_context(patch.object(jetson.select, 'select', return_value=([10], [], [])))
                read = stack.enter_context(patch.object(jetson.os, 'read'))
                if isinstance(failure, OSError):
                    read.side_effect = failure
                else:
                    read.return_value = failure
                stop = stack.enter_context(patch.object(daemon, 'stop_audio'))
                self.assertEqual(daemon.run(), 1)
                self.assertIn('ep0', daemon.last_error)
                stop.assert_called_once_with()
                read.assert_called_once()


class EndpointRecoveryTests(unittest.TestCase):
    def assert_restart_requested(self, daemon):
        self.assertTrue(daemon.stopping.is_set())
        self.assertFalse(daemon.active.is_set())
        self.assertEqual(daemon.exit_code, 1)
        self.assertEqual(daemon.errors, 1)
        self.assertIn('重启服务', daemon.last_error)

    def test_failed_in_write_requests_service_restart(self):
        daemon = jetson.Daemon()
        daemon.active.set()
        read_fd, write_fd = os.pipe()
        os.write(write_fd, PAYLOAD)
        os.close(write_fd)
        stream = os.fdopen(read_fd, 'rb', buffering=0)
        self.addCleanup(stream.close)
        daemon.mic = types.SimpleNamespace(stdout=stream)
        with patch.object(daemon, 'drain_capture_backlog'), \
                patch.object(daemon, 'pipe_backlog', return_value=2048), \
                patch.object(jetson.os, 'write', side_effect=OSError('endpoint disconnected')):
            daemon.mic_pump(daemon.generation)
        self.assert_restart_requested(daemon)

    def test_short_in_write_requests_restart(self):
        daemon = jetson.Daemon()
        daemon.active.set()
        size = proto.FAST_BYTES if jetson.FAST else proto.FRAME_BYTES
        daemon.mic = types.SimpleNamespace(stdout=io.BytesIO(b'\x01\x00' * (size // 2)))
        with patch.object(daemon, 'drain_capture_backlog'), \
                patch.object(jetson.os, 'write', return_value=1):
            daemon.mic_pump(daemon.generation)
        self.assert_restart_requested(daemon)
        self.assertEqual(daemon.frames_in, 0)

    def test_open_failure_requests_restart_and_closes_partial_endpoints(self):
        for opens, closed in (([OSError('ep1 lost')], []), ([10, OSError('ep2 lost')], [10])):
            with self.subTest(opens=opens):
                daemon = jetson.Daemon()
                with patch.object(jetson.os, 'open', side_effect=opens), \
                        patch.object(jetson.os, 'close') as close:
                    daemon.enable()
                self.assert_restart_requested(daemon)
                self.assertEqual([call.args[0] for call in close.call_args_list], closed)

    def test_out_eof_requests_restart(self):
        daemon = jetson.Daemon()
        daemon.active.set()
        daemon.ep_out = 10
        with patch.object(jetson.select, 'select', return_value=([10], [], [])), \
                patch.object(jetson.os, 'read', return_value=b''):
            daemon.usb_pump(daemon.generation)
        self.assert_restart_requested(daemon)

    def test_failed_out_read_requests_service_restart(self):
        daemon = jetson.Daemon()
        daemon.active.set()
        daemon.ep_out = 10
        with patch.object(jetson.select, 'select', return_value=([10], [], [])), \
                patch.object(jetson.os, 'read', side_effect=OSError('endpoint disconnected')):
            daemon.usb_pump(daemon.generation)
        self.assert_restart_requested(daemon)

    def test_failed_out_poll_requests_service_restart(self):
        daemon = jetson.Daemon()
        daemon.active.set()
        daemon.ep_out = 10
        with patch.object(jetson.select, 'select', side_effect=OSError('bad descriptor')):
            daemon.usb_pump(daemon.generation)
        self.assert_restart_requested(daemon)

    def test_watchdog_restarts_stalled_service(self):
        daemon = jetson.Daemon()
        daemon.active.set()
        daemon.last_frame_at = 1.0
        with patch.object(jetson.time, 'sleep'), \
                patch.object(jetson.time, 'monotonic', return_value=5.0):
            daemon.watchdog()
        self.assert_restart_requested(daemon)

    def test_watchdog_detects_stalled_first_frame(self):
        daemon = jetson.Daemon()
        with patch.object(jetson.os, 'open', side_effect=[10, 11]), \
                patch.object(jetson.threading.Thread, 'start'), \
                patch.object(jetson.time, 'monotonic', return_value=1.0):
            daemon.enable()
        with patch.object(jetson.time, 'sleep'), \
                patch.object(jetson.time, 'monotonic', return_value=5.0):
            daemon.watchdog()
        self.assert_restart_requested(daemon)

    def test_run_cleans_up_and_returns_failure(self):
        daemon = jetson.Daemon()
        daemon.request_restart('端点故障，重启服务')
        with contextlib.ExitStack() as stack:
            for obj, name in ((jetson.signal, 'signal'), (jetson.threading.Thread, 'start'),
                              (daemon, 'start_audio')):
                stack.enter_context(patch.object(obj, name))
            stack.enter_context(patch.dict(os.environ, {'MOZART_UDC': ''}))
            stack.enter_context(patch.object(jetson.os, 'open', return_value=10))
            stack.enter_context(patch.object(jetson.os, 'write'))
            close = stack.enter_context(patch.object(jetson.os, 'close'))
            stop = stack.enter_context(patch.object(daemon, 'stop_audio'))
            self.assertEqual(daemon.run(), 1)
        stop.assert_called_once_with()
        close.assert_called_once_with(10)
        unit = (USB_ROOT / 'systemd/mozart-usb-audio-jetson.service').read_text()
        self.assertIn('Restart=always', unit)


class PipeFeedbackTests(unittest.TestCase):
    def test_pcm_block_handles_partial_pipe_reads(self):
        stream = Mock()
        stream.read.side_effect = [b'ab', b'c', b'de']
        self.assertEqual(read_block(stream, 5), b'abcde')
        self.assertEqual([c.args[0] for c in stream.read.call_args_list], [5, 3, 2])

    def test_pcm_block_returns_short_data_at_eof(self):
        self.assertEqual(read_block(io.BytesIO(b'ab'), 5), b'ab')

    def test_host_return_uses_real_pipe_sample_frames(self):
        bridge = host.Bridge()
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        os.write(write_fd, PAYLOAD * 3)
        bridge.speaker_fd = write_fd
        with patch.object(bridge.return_src, 'ratio', wraps=bridge.return_src.ratio) as ratio, \
                patch.object(bridge, 'write_speaker'):
            bridge.play_pcm(PAYLOAD)
        ratio.assert_called_once_with(3 * proto.FAST_SAMPLES)

    def test_capture_preserves_pcm_without_resampling(self):
        for module, kind in ((host, 'host'), (jetson, 'jetson')):
            with self.subTest(daemon=kind):
                bridge = module.Bridge() if kind == 'host' else module.Daemon()
                read_fd, write_fd = os.pipe()
                os.write(write_fd, PAYLOAD * 3)
                with os.fdopen(read_fd, 'rb', buffering=0) as stream:
                    os.close(write_fd)
                    if kind == 'host':
                        bridge.capture = types.SimpleNamespace(stdout=stream)
                        bridge.ep_out = Mock()
                        bridge.ep_out.write.side_effect = lambda *a, **kw: bridge.stopping.set()
                    else:
                        bridge.mic = types.SimpleNamespace(stdout=stream)
                        bridge.active.set()
                    with patch.object(bridge, 'drain_capture_backlog'), \
                            patch.object(module.os, 'write', side_effect=lambda *a: (
                                bridge.stopping.set(), len(a[1]))[1]) as write:
                        if kind == 'host':
                            bridge.alsa_to_usb()
                        else:
                            bridge.mic_pump(bridge.generation)
                    frame = bridge.ep_out.write.call_args.args[0] if kind == 'host' else write.call_args.args[1]
                    self.assertEqual(proto.decode(frame)[-1], PAYLOAD)

    def test_silence_writes_use_the_playback_lock(self):
        for module, bridge in ((host, host.Bridge()), (jetson, jetson.Daemon())):
            with self.subTest(module=module.__name__):
                bridge.speaker_fd = 20
                def write(_):
                    self.assertTrue(bridge.return_lock.locked())
                    bridge.stopping.set()
                with patch.object(bridge, 'write_speaker', side_effect=write), \
                        patch.object(module.time, 'sleep'):
                    bridge.silencer()

    def test_pipe_feedback_failure_records_the_fallback_reason(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        os.close(write_fd)
        for bridge in (host.Bridge(), jetson.Daemon()):
            self.assertEqual(bridge.pipe_backlog(read_fd), 0)
            self.assertEqual(bridge.pipe_backlog(read_fd), 0)
            self.assertTrue(bridge.backlog_error)
            self.assertIn('查询管道水位失败', bridge.last_error)
            self.assertEqual(bridge.errors, 1)

    def test_jetson_return_uses_real_pipe_sample_frames(self):
        daemon = jetson.Daemon()
        daemon.active.set()
        daemon.ep_out = 10
        daemon.speaker_fd = 20
        frame = proto.encode(proto.TYPE_PCM, 0, 0, PAYLOAD)
        with patch.object(jetson.select, 'select', return_value=([10], [], [])), \
                patch.object(jetson.os, 'read', return_value=frame), \
                patch.object(daemon, 'pipe_backlog', return_value=len(PAYLOAD) * 3), \
                patch.object(daemon.return_src, 'ratio', wraps=daemon.return_src.ratio) as ratio, \
                patch.object(daemon, 'write_speaker', side_effect=lambda _: daemon.stopping.set()):
            daemon.usb_pump(daemon.generation)
        ratio.assert_called_once_with(3 * proto.FAST_SAMPLES)


class FrameSizeCompatTests(unittest.TestCase):
    """接收方按帧长处理，兼容两种发送档位。"""

    def test_audio_predicate_accepts_both_streams(self):
        self.assertTrue(proto.is_audio(proto.TYPE_PCM, proto.STREAM_AUDIO))
        self.assertTrue(proto.is_audio(proto.TYPE_PCM, proto.STREAM_FAST))
        self.assertFalse(proto.is_audio(proto.TYPE_CONTROL, proto.STREAM_FAST))
        self.assertFalse(proto.is_audio(proto.TYPE_PCM, proto.STREAM_CONTROL))

    def test_watermark_target_follows_frame_size(self):
        frame_20ms = proto.RATE * proto.FRAME_MS // 1000
        src = host.AsyncSrc(channels=proto.CHANNELS,
                            target_frames=proto.FAST_SAMPLES * host.TARGET_BLOCKS)
        src.observe(b'\x00' * proto.FRAME_BYTES)
        self.assertEqual(src.target, float(frame_20ms * host.TARGET_BLOCKS))
        src.observe(b'\x00' * proto.FAST_BYTES)
        self.assertEqual(src.target, float(proto.FAST_SAMPLES * host.TARGET_BLOCKS))
        src.observe(b'')
        self.assertEqual(src.target, float(proto.FAST_SAMPLES * host.TARGET_BLOCKS))

    def test_descriptors_declare_both_interface_endpoints(self):
        blob = jetson.descriptors()
        for address in (proto.EP_AUDIO_OUT, proto.EP_AUDIO_IN,
                        proto.EP_FAST_OUT, proto.EP_FAST_IN):
            self.assertEqual(blob.count(bytes([7, 5, address, 2])), 2)

    def test_stream_id_matches_frame_size(self):
        bridge = host.Bridge()
        bridge.capture = types.SimpleNamespace(
            stdout=io.BytesIO(b'\x10\x00' * (proto.FAST_SAMPLES * proto.CHANNELS)))
        frames = []

        class Endpoint:
            def write(self, frame, timeout=0):
                frames.append(frame)
                bridge.stopping.set()
                return len(frame)

        bridge.ep_out = Endpoint()
        with patch.object(host, 'DIRECT', False), \
                patch.object(bridge, 'drain_capture_backlog'):
            bridge.alsa_to_usb()
        version, kind, stream, _flags, _seq, _ts, payload = proto.decode(frames[0])
        self.assertEqual(stream, proto.STREAM_FAST)
        self.assertEqual(len(payload), proto.FAST_BYTES)

    def test_host_receives_both_stream_ids(self):
        bridge = host.Bridge()
        frames = (
            proto.encode(proto.TYPE_PCM, 0, 0, b'\x01' * proto.FAST_BYTES,
                         stream=proto.STREAM_FAST)
            + proto.encode(proto.TYPE_PCM, 1, 0, b'\x02' * proto.FRAME_BYTES,
                           stream=proto.STREAM_AUDIO)
        )

        class Endpoint:
            def __init__(self):
                self.reads = [frames]

            def read(self, _size, timeout=0):
                bridge.stopping.set()
                return self.reads.pop(0) if self.reads else b''

        bridge.ep_in = Endpoint()
        played = []
        with patch.object(bridge, 'play_pcm', side_effect=played.append):
            bridge.usb_to_alsa()
        self.assertEqual([len(item) for item in played], [proto.FAST_BYTES, proto.FRAME_BYTES])

    def test_audio_frame_with_invalid_length_is_skipped(self):
        bridge = host.Bridge()
        frame = proto.encode(proto.TYPE_PCM, 0, 0, b'\x01' * 4, stream=proto.STREAM_AUDIO)
        bridge.ep_in = types.SimpleNamespace(read=lambda _size, timeout=0: (
            bridge.stopping.set() or frame))
        played = []
        with patch.object(bridge, 'play_pcm', side_effect=played.append):
            bridge.usb_to_alsa()
        self.assertEqual(played, [])


if __name__ == '__main__':
    unittest.main()
