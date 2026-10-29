import importlib.util
import io
import json
import pathlib
import tempfile
import re
import socket
import struct
import sys
import types
import unittest
from unittest.mock import Mock, patch

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

# 加载 stt_service，不加载 sherpa 实模型。
_spec = importlib.util.spec_from_file_location('stt_telemetry', TOOLS/'stt_service.py')
module = importlib.util.module_from_spec(_spec)
with patch.dict(sys.modules, {'sherpa_onnx': types.ModuleType('sherpa_onnx')}):
    _spec.loader.exec_module(module)

REPORT = re.compile(r'\[stt\] packets=(\d+) delta=(\d+) rejected=(\d+) finals=(\d+)')
EXIT = re.compile(r'\[stt\] exit: packets=(\d+) finals=(\d+)')


def valid_packet(seg=1, magic=module.MZRT_MAGIC):
    """1300B 契约包：20B 头 + 320 个 float32 样本。"""
    return (module.HEADER.pack(magic, 0, 0, 1, 1, 1, seg)
            + struct.pack(f'<{module.FRAME_SAMPLES}f', *([0.0]*module.FRAME_SAMPLES)))


class Clock:
    """假 monotonic 时钟：每交付一个 recv 项前进 step 秒。"""
    def __init__(self, start=1000.0, step=1.0):
        self.now, self.step = start, step

    def __call__(self):
        return self.now


def run_service(items, step=1.0, status_file=None, recognizer=None, stdout=None,
                json_mode=True, extra_args=()):
    """用有限 recv 序列和假时钟运行 main，返回 (stderr 文本, 周期日志, exit 日志)。"""
    clock, items = Clock(step=step), iter(items)

    def recvfrom(_size):
        item = next(items)
        clock.now += clock.step
        if isinstance(item, BaseException):
            raise item
        return item, None

    sock = Mock()
    sock.recvfrom.side_effect = recvfrom
    if recognizer is None:
        recognizer = Mock()
        recognizer.is_ready.return_value = False
        recognizer.is_endpoint.return_value = False
        recognizer.get_result.return_value = ''
    stderr = io.StringIO()
    with patch.dict(module.os.environ, {'MOZART_STT_STATUS_JSON': str(status_file) if status_file else '',
                                         'MOZART_STACK_INSTANCE_ID': 'telemetry-test'}), \
            patch.object(module, 'build_recognizer', return_value=recognizer), \
            patch.object(module.socket, 'socket', return_value=sock), \
            patch.object(module.signal, 'signal'), \
            patch.object(module.time, 'monotonic', clock), \
            patch.object(sys, 'argv', ['stt', '--model', 'fake'] + (['--json'] if json_mode else []) + list(extra_args)), \
            patch.object(sys, 'stdout', stdout if stdout is not None else io.StringIO()), \
            patch.object(sys, 'stderr', stderr):
        module.main()
    text = stderr.getvalue()
    reports = [tuple(int(v) for v in m) for m in REPORT.findall(text)]
    exits = [tuple(int(v) for v in m) for m in EXIT.findall(text)]
    return text, reports, exits


class SttTelemetryTests(unittest.TestCase):
    def test_continuous_valid_packets_report_packets_and_delta(self):
        text, reports, exits = run_service([valid_packet()]*20 + [KeyboardInterrupt()])
        # 10 秒一次：第 11 个包时报告前 10 个包，第 21 个包时再报告 10 个增量包。
        self.assertEqual(reports, [(10, 10, 0, 0), (20, 10, 0, 0)])
        self.assertEqual(exits, [(20, 0)])
        self.assertIn('pending_seg=1', text)

    def test_report_waits_for_full_ten_seconds(self):
        text, reports, exits = run_service([valid_packet()]*9 + [KeyboardInterrupt()])
        self.assertEqual(reports, [])
        self.assertEqual(exits, [(9, 0)])
        self.assertNotIn('packets=9 delta=', text)

    def test_timeout_without_packets_keeps_counters_zero(self):
        _, reports, exits = run_service([socket.timeout()]*10 + [KeyboardInterrupt()])
        self.assertEqual(reports, [(0, 0, 0, 0)])
        self.assertEqual(exits, [(0, 0)])

    def test_wrong_length_packets_are_rejected(self):
        _, reports, exits = run_service([valid_packet()[:-1]]*10 + [KeyboardInterrupt()])
        self.assertEqual(reports, [(0, 0, 10, 0)])
        self.assertEqual(exits, [(0, 0)])

    def test_status_file_records_valid_audio_and_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)/'audio-status.json'
            run_service([valid_packet()]*10 + [KeyboardInterrupt()], status_file=path)
            status = json.loads(path.read_text())
            self.assertEqual(status['packets'], 10)
            self.assertEqual(status['rejected'], 0)
            self.assertIsInstance(status['last_audio_at'], float)
            self.assertEqual(status['instance_id'], 'telemetry-test')

    def test_rejected_packets_do_not_declare_audio_available(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)/'audio-status.json'
            run_service([valid_packet()[:-1]]*10 + [KeyboardInterrupt()], status_file=path)
            status = json.loads(path.read_text())
            self.assertEqual(status['packets'], 0)
            self.assertEqual(status['rejected'], 10)
            self.assertIsNone(status['last_audio_at'])

    def test_text_content_preserves_short_words_and_numbers(self):
        for text in ('', ' ', '\n\t', '。', '.', '…', '！？', '()', '——', '【】', ' 。 . '):
            with self.subTest(text=text):
                self.assertFalse(module.has_text_content(text))
        for text in ('不', '对', '了', '0', 'A', 'I', '3.14', '-1', '你好。', '$', '+'):
            with self.subTest(text=text):
                self.assertTrue(module.has_text_content(text))

    def test_punctuation_is_filtered_before_partial_and_final_delivery(self):
        results = iter(['。', '.', '…', '！？', '()', ' ', '不', '0', '对', '你好。'])
        current = ['']
        recognizer = Mock()
        stream = recognizer.create_stream.return_value
        stream.accept_waveform.side_effect = lambda *_: current.__setitem__(0, next(results, ''))
        recognizer.is_ready.return_value = False
        recognizer.is_endpoint.return_value = True
        recognizer.get_result.side_effect = lambda *_: current[0]
        recognizer.reset.side_effect = lambda *_: current.__setitem__(0, '')
        output = io.StringIO()
        refiner = Mock()
        with tempfile.TemporaryDirectory() as directory, patch.object(module, 'UtteranceRefiner', return_value=refiner):
            status_path = pathlib.Path(directory)/'audio-status.json'
            text, _, exits = run_service([valid_packet()]*10 + [KeyboardInterrupt()],
                                          status_file=status_path, recognizer=recognizer, stdout=output,
                                          extra_args=['--final-model', 'fake'])
            status = json.loads(status_path.read_text())
        events = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
        finals = [event for event in events if event['type'] == 'final']
        partials = [event for event in events if event['type'] == 'partial']
        self.assertEqual([event['text_raw'] for event in finals], ['不', '0', '对', '你好。'])
        self.assertEqual([event['seq'] for event in finals], [1, 2, 3, 4])
        self.assertEqual([event['text'] for event in partials], ['不', '0', '对', '你好。'])
        self.assertEqual([event['seq'] for event in partials], [1, 2, 3, 4])
        self.assertEqual([call.args[0]['text_raw'] for call in refiner.submit.call_args_list],
                         ['不', '0', '对', '你好。'])
        refiner.close.assert_called_once()
        self.assertEqual(status['punctuation_finals_skipped'], 5)
        self.assertEqual(text.count('skipped punctuation-only final:'), 5)
        self.assertEqual(exits, [(10, 4)])

    def test_plain_console_filters_punctuation_and_keeps_zero(self):
        for value, expected in [('。', False), ('0', True)]:
            with self.subTest(value=value):
                recognizer = Mock()
                recognizer.is_ready.return_value = False
                recognizer.is_endpoint.return_value = False
                recognizer.get_result.return_value = value
                output = io.StringIO()
                run_service([valid_packet(), KeyboardInterrupt()], recognizer=recognizer,
                            stdout=output, json_mode=False)
                self.assertEqual('[PART]' in output.getvalue(), expected)

    def test_timeout_reopen_and_cumulative_text_keep_sequence(self):
        results = iter(['。', '…', '0', '0。', '不', '不对'])
        current = ['']
        recognizer = Mock()
        recognizer.is_ready.return_value = False
        recognizer.is_endpoint.return_value = False
        recognizer.get_result.side_effect = lambda *_: current[0]

        def create_stream():
            current[0] = ''
            stream = Mock()

            def accept(_rate, pcm):
                if len(pcm) == module.FRAME_SAMPLES:
                    current[0] = next(results)

            stream.accept_waveform.side_effect = accept
            return stream

        recognizer.create_stream.side_effect = create_stream
        output = io.StringIO()
        items = ([valid_packet()]*2 + [socket.timeout()]*2
                 + [valid_packet()]*2 + [socket.timeout()]*2
                 + [valid_packet()]*2 + [KeyboardInterrupt()])
        text, _, exits = run_service(items, recognizer=recognizer, stdout=output)
        events = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
        finals = [event for event in events if event['type'] == 'final']
        self.assertEqual([event['text_raw'] for event in finals], ['0。', '不对'])
        self.assertEqual([event['seq'] for event in finals], [1, 2])
        self.assertEqual([event['seq'] for event in events if event['type'] == 'partial'], [1, 1, 2, 2])
        self.assertEqual(recognizer.create_stream.call_count, 3)
        self.assertEqual(text.count('skipped punctuation-only final:'), 1)
        self.assertEqual(exits, [(6, 2)])

    def test_wrong_magic_packets_are_rejected(self):
        bad = valid_packet(magic=0xDEADBEEF)
        self.assertEqual(len(bad), 1300)
        _, reports, exits = run_service([bad]*10 + [KeyboardInterrupt()])
        self.assertEqual(reports, [(0, 0, 10, 0)])
        self.assertEqual(exits, [(0, 0)])


if __name__ == '__main__':
    unittest.main()
