from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from boundary_classifier import BoundaryWorker, choose_boundary, classifier_prefix
from stable_clauses import StableClauseDispatcher


TEXT = '昨天我们完成了测试今天继续处理剩余任务'


class BlockedModel:
    def __init__(self, fail=False):
        self.entered, self.release = threading.Event(), threading.Event()
        self.fail = fail

    def predict(self, text, committed):
        self.entered.set()
        if not self.release.wait(3):
            raise RuntimeError('Test model was not released')
        if self.fail:
            raise ValueError('Model inference failed')
        return {'cut': {'end': len(committed)+9, 'score': .9, 'reason': 'model'}, 'elapsed_ms': 1}


class BoundaryClassifierTests(unittest.TestCase):
    def test_waiting_requests_coalesce_and_replaced_source_can_retry(self):
        model, received, third, done = BlockedModel(), [], threading.Event(), threading.Event()
        def callback(event, result):
            received.append(event['seq'])
            if event['seq'] == 3:
                third.set()
            if event['seq'] == 2:
                done.set()
        worker = BoundaryWorker(model, callback)
        try:
            worker.submit('1', {'seq': 1}, TEXT, '')
            self.assertTrue(model.entered.wait(3))
            worker.submit('2', {'seq': 2}, TEXT, '')
            worker.submit('3', {'seq': 3}, TEXT, '')
            self.assertEqual(worker.pending.qsize(), 1)
            self.assertNotIn('2', worker.seen)
            model.release.set()
            self.assertTrue(third.wait(3))
            worker.submit('2', {'seq': 2}, TEXT, '')
            self.assertTrue(done.wait(3))
        finally:
            model.release.set()
            worker.close()
        self.assertEqual(received, [1, 3, 2])

    def test_boundary_can_use_unpunctuated_text_and_length_limit(self):
        self.assertEqual(choose_boundary(TEXT, '', {9: .9})['end'], 9)
        long_text = TEXT*3
        decision = choose_boundary(long_text, '', {16: .4}, max_chars=32)
        self.assertEqual(decision, {'end': 16, 'score': .4, 'reason': 'length_limit'})
        self.assertIsNone(choose_boundary(TEXT, '', {len(TEXT): 1}))
        mixed = '昨天我们已经检查alpha1234567890今天继续'
        inside = mixed.index('alpha')+2
        self.assertIsNone(choose_boundary(mixed, '', {inside: 1}, max_chars=32))

    def test_classifier_requires_repeated_prefix_and_observed_tail(self):
        self.assertIsNone(classifier_prefix([[0, TEXT]], ''))
        self.assertIsNone(classifier_prefix([[0, TEXT], [.1, TEXT]], ''))
        self.assertEqual(classifier_prefix([[0, TEXT], [.25, TEXT+'一']], ''), TEXT)
        self.assertIsNone(classifier_prefix([[0, TEXT], [.25, TEXT]], '不同前文'))

    def test_slow_classifier_does_not_hold_captions_or_overwrite_new_source(self):
        model, rows, committed = BlockedModel(), [], threading.Event()
        def publish(row):
            rows.append(row)
            if row.get('translation_segments'):
                committed.set()
        with tempfile.TemporaryDirectory() as directory, \
                patch('subtitle_bridge.translate_recognized_event', return_value=('Translation.', .1)):
            dispatcher = StableClauseDispatcher(publish, 's', 'translator',
                spool_path=Path(directory)/'queue.sqlite3', boundary_classifier=model)
            try:
                dispatcher.accept({'type': 'partial', 'seq': 1, 'text': TEXT, 'emitted_at': 0})
                dispatcher.accept({'type': 'partial', 'seq': 1, 'text': TEXT+'一', 'emitted_at': .25})
                self.assertTrue(model.entered.wait(3))
                extended = TEXT+'一些后续内容'
                dispatcher.accept({'type': 'partial', 'seq': 1, 'text': extended, 'emitted_at': .5})
                self.assertEqual(rows[-1]['zh'], extended)
                model.release.set()
                self.assertTrue(committed.wait(3))
                self.assertEqual(rows[-1]['zh'], extended)
                dispatcher.accept({'type': 'final', 'seq': 1, 'text': extended, 'emitted_at': 1})
            finally:
                model.release.set()
                self.assertTrue(dispatcher.close())
        self.assertEqual(''.join(p['source_text'] for p in rows[-1]['translation_segments']), extended)
        self.assertEqual(rows[-1]['translation_status'], 'completed')
        self.assertTrue(rows[-1]['translation_segments'][0]['early'])
        self.assertEqual(rows[-1]['translation_segments'][0]['boundary_classifier']['cut']['reason'], 'model')

    def test_final_result_rejects_late_classification(self):
        model, rows = BlockedModel(), []
        with patch('subtitle_bridge.translate_recognized_event', return_value=('Translation.', .1)):
            dispatcher = StableClauseDispatcher(rows.append, 's', 'translator', boundary_classifier=model)
            try:
                for at in (0, .25):
                    dispatcher.accept({'type': 'partial', 'seq': 1, 'text': TEXT, 'emitted_at': at})
                self.assertTrue(model.entered.wait(3))
                dispatcher.accept({'type': 'final', 'seq': 1, 'text': TEXT, 'emitted_at': .5})
            finally:
                model.release.set()
                self.assertTrue(dispatcher.close())
        self.assertEqual(len(rows[-1]['translation_segments']), 1)
        self.assertFalse(rows[-1]['translation_segments'][0]['early'])

    def test_revised_source_rejects_stale_classification(self):
        rows = []
        with patch('subtitle_bridge.translate_recognized_event', return_value=('Translation.', .1)):
            dispatcher = StableClauseDispatcher(rows.append, 's', 'translator')
            for at in (0, .25):
                dispatcher.accept({'type': 'partial', 'seq': 1, 'text': TEXT, 'emitted_at': at})
            revised = TEXT.replace('完成了', '没有完成')
            dispatcher.accept({'type': 'partial', 'seq': 1, 'text': revised, 'emitted_at': .5})
            dispatcher.accept({'type': 'partial', 'seq': 1, 'text': TEXT, 'emitted_at': .25},
                _classified={'prefix': TEXT, 'committed': '', 'cut': {'end': 9}})
            self.assertEqual(rows[-1]['zh'], revised)
            self.assertEqual(rows[-1]['translation_segments'], [])
            dispatcher.accept({'type': 'final', 'seq': 1, 'text': revised, 'emitted_at': 1})
            self.assertTrue(dispatcher.close())

    def test_classifier_error_keeps_final_translation_and_source_visible(self):
        model, rows, warning = BlockedModel(fail=True), [], threading.Event()
        def publish(row):
            rows.append(row)
            if row.get('boundary_classifier_warning'):
                warning.set()
        with patch('subtitle_bridge.translate_recognized_event', return_value=('Translation.', .1)):
            dispatcher = StableClauseDispatcher(publish, 's', 'translator', boundary_classifier=model)
            try:
                for at in (0, .25):
                    dispatcher.accept({'type': 'partial', 'seq': 1, 'text': TEXT, 'emitted_at': at})
                self.assertTrue(model.entered.wait(3))
                model.release.set()
                self.assertTrue(warning.wait(3))
                self.assertEqual(rows[-1]['zh'], TEXT)
                dispatcher.accept({'type': 'final', 'seq': 1, 'text': TEXT, 'emitted_at': .5})
            finally:
                model.release.set()
                self.assertTrue(dispatcher.close())
        self.assertEqual(rows[-1]['translation_status'], 'completed')
        self.assertEqual(rows[-1]['translation_segments'][0]['source_text'], TEXT)


if __name__ == '__main__':
    unittest.main()
