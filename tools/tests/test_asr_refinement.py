import pathlib
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from asr_refinement import UtteranceRefiner
from subtitle_bridge import CaptionDispatcher


class RefinementTests(unittest.TestCase):
    def test_slow_model_initialization_does_not_block_submission(self):
        entered, release = threading.Event(), threading.Event()
        rows = []
        def build():
            entered.set()
            release.wait(3)
            return Mock()
        with patch('asr_refinement.refine_with_numeric_check', return_value=('你好。', None)):
            refiner = UtteranceRefiner(build, rows.append, pending_limit=1)
            try:
                self.assertTrue(entered.wait(3))
                refiner.submit({'seq': 1, 'text': '你好'}, [(0.0,)])
                refiner.submit({'seq': 2, 'text': '下一句'}, [(0.0,)])
                self.assertEqual(rows[-1]['seq'], 2)
                self.assertEqual(rows[-1]['refinement_status'], 'skipped')
            finally:
                release.set()
                self.assertTrue(refiner.close(3))
        completed = [r for r in rows if r['refinement_status'] == 'completed']
        self.assertEqual([r['seq'] for r in completed], [1])
        self.assertEqual(completed[0]['text'], '你好')
        self.assertEqual(completed[0]['refined_text'], '你好。')

    def test_initialization_failure_keeps_online_text(self):
        rows = []
        refiner = UtteranceRefiner(Mock(side_effect=RuntimeError('Missing model')), rows.append)
        refiner.submit({'seq': 1, 'text': '在线结果'}, [(0.0,)])
        self.assertTrue(refiner.close(3))
        self.assertEqual(rows[-1]['text'], '在线结果')
        self.assertEqual(rows[-1]['refinement_status'], 'failed')
        self.assertIn('Missing model', rows[-1]['refinement_error'])

    def test_refinement_and_translation_updates_do_not_replace_each_other(self):
        entered, release = threading.Event(), threading.Event()
        rows = []
        def translate(*args, **kwargs):
            entered.set()
            release.wait(3)
            return 'Hello.', .1
        with patch('subtitle_bridge.translate_recognized_event', side_effect=translate) as translate_call, \
                patch('subtitle_bridge.request_json', return_value={'id': 'speech-job'}) as speech_call:
            dispatcher = CaptionDispatcher(rows.append, 'session', 'http://example', 'http://speech')
            try:
                dispatcher.accept({'type': 'final', 'seq': 1, 'text': '你好'})
                self.assertTrue(entered.wait(3))
                dispatcher.accept({'type': 'refined', 'seq': 1, 'text': '你好',
                    'refined_text': '你好。', 'refinement_status': 'completed',
                    'asr_numeric_audit': {'issues': ['example']}})
                self.assertEqual(rows[-1]['zh'], '你好')
                self.assertEqual(rows[-1]['refined_zh'], '你好。')
            finally:
                release.set()
                self.assertTrue(dispatcher.close(3))
        self.assertEqual(rows[-1]['refined_zh'], '你好。')
        self.assertEqual(rows[-1]['en'], 'Hello.')
        self.assertEqual(rows[-1]['translation_source_text'], '你好')
        self.assertEqual(rows[-1]['speech'], 'speech-job')
        self.assertEqual([r['revision'] for r in rows], list(range(len(rows))))
        translate_call.assert_called_once()
        speech_call.assert_called_once()

    def test_shutdown_marks_pending_refinement_and_prevents_late_output(self):
        entered, release = threading.Event(), threading.Event()
        rows = []
        def refine(*args):
            entered.set()
            release.wait(3)
            return '复核结果', None
        with patch('asr_refinement.refine_with_numeric_check', side_effect=refine):
            refiner = UtteranceRefiner(Mock(), rows.append)
            try:
                refiner.submit({'seq': 1, 'text': '在线结果'}, [(0.0,)])
                self.assertTrue(entered.wait(3))
                self.assertFalse(refiner.close(0))
                self.assertEqual(rows[-1]['refinement_status'], 'skipped')
                count = len(rows)
            finally:
                release.set()
                refiner.worker.join(3)
        self.assertEqual(len(rows), count)


if __name__ == '__main__':
    unittest.main()
