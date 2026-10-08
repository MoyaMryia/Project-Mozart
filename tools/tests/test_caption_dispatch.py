import json
import io
import pathlib
import struct
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from subtitle_bridge import CaptionDispatcher


class CaptionDispatchTests(unittest.TestCase):
    def test_slow_translation_does_not_hold_next_source_or_partial(self):
        entered, release = threading.Event(), threading.Event()
        rows = []
        def translate(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return 'Hello.', .1
        with patch('subtitle_bridge.translate_recognized_event', side_effect=translate):
            dispatcher = CaptionDispatcher(rows.append, 'session', 'http://example')
            try:
                dispatcher.accept({'type': 'partial', 'seq': 1, 'text': '你'})
                dispatcher.accept({'type': 'final', 'seq': 1, 'text': '你好。'})
                self.assertTrue(entered.wait(3))
                dispatcher.accept({'type': 'partial', 'seq': 2, 'text': '下一句'})
                dispatcher.accept({'type': 'final', 'seq': 2, 'text': '下一句。'})
                self.assertEqual([r['zh'] for r in rows], ['你', '你好。', '下一句', '下一句。'])
                dispatcher.accept({'type': 'partial', 'seq': 1, 'text': '过期'})
                self.assertEqual(len(rows), 4)
            finally:
                release.set()
                self.assertTrue(dispatcher.close(3))
        first = [r for r in rows if r['utterance_id'] == 'session:1']
        self.assertEqual([r['revision'] for r in first], [0, 1, 2])
        self.assertEqual(first[-1]['en'], 'Hello.')

    def test_full_translation_queue_retains_source_and_preserves_fifo(self):
        entered, release = threading.Event(), threading.Event()
        rows, translated = [], []
        def translate(url, event, **kwargs):
            translated.append(event['seq'])
            entered.set()
            self.assertTrue(release.wait(3))
            return 'Hello.', .1
        with patch('subtitle_bridge.translate_recognized_event', side_effect=translate):
            dispatcher = CaptionDispatcher(rows.append, 'session', 'http://example', pending_limit=1)
            try:
                dispatcher.accept({'type': 'final', 'seq': 1, 'text': '第一句。'})
                self.assertTrue(entered.wait(3))
                dispatcher.accept({'type': 'final', 'seq': 2, 'text': '第二句。'})
                dispatcher.accept({'type': 'final', 'seq': 3, 'text': '第三句。'})
                self.assertEqual(rows[-1]['zh'], '第三句。')
                self.assertEqual(rows[-1]['translation_status'], 'skipped')
                dispatcher.accept({'type': 'partial', 'seq': 4, 'text': '继续识别'})
                self.assertEqual(rows[-1]['translation_status'], 'recognizing')
            finally:
                release.set()
                dispatcher.close(3)
        self.assertEqual(translated, [1, 2])

    def test_shutdown_deadline_prevents_writes_to_closed_output(self):
        entered, release = threading.Event(), threading.Event()
        rows = []
        def translate(*args, **kwargs):
            entered.set()
            release.wait(3)
            return 'Hello.', .1
        with patch('subtitle_bridge.translate_recognized_event', side_effect=translate), \
                patch('subtitle_bridge.request_json') as speech_call:
            dispatcher = CaptionDispatcher(rows.append, 'session', 'http://example', 'http://speech')
            try:
                dispatcher.accept({'type': 'final', 'seq': 1, 'text': '你好。'})
                self.assertTrue(entered.wait(3))
                self.assertFalse(dispatcher.close(0))
                self.assertEqual(rows[-1]['translation_status'], 'skipped')
                count = len(rows)
            finally:
                release.set()
                dispatcher.worker.join(3)
        self.assertEqual(len(rows), count)
        speech_call.assert_not_called()

    def test_stt_partial_and_final_have_the_same_sequence(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('stt_test', pathlib.Path(__file__).resolve().parents[1]/'stt_service.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'sherpa_onnx': types.ModuleType('sherpa_onnx')}):
            spec.loader.exec_module(module)
        recognizer, sock = Mock(), Mock()
        recognizer.is_ready.return_value = False
        recognizer.is_endpoint.return_value = True
        recognizer.get_result.side_effect = ['你好', '你好', '']
        packet = module.HEADER.pack(module.MZRT_MAGIC, 1, 0, 1, 1, 1, 0)+struct.pack('<320f', *([0.0]*320))
        sock.recvfrom.side_effect = [(packet, None), KeyboardInterrupt()]
        output = io.StringIO()
        with patch.object(module, 'build_recognizer', return_value=recognizer), \
                patch.object(module.socket, 'socket', return_value=sock), \
                patch.object(module.signal, 'signal'), \
                patch.object(sys, 'argv', ['stt', '--model', 'fake', '--json']), \
                patch.object(sys, 'stdout', output):
            module.main()
        events = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
        self.assertEqual([e['type'] for e in events], ['partial', 'final'])
        self.assertEqual([e['seq'] for e in events], [1, 1])


if __name__ == '__main__':
    unittest.main()
