import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stable_clauses import StableClauseDispatcher, stable_cut


PREFIX = '昨天我们已经完成了这个方案'
TEXT = PREFIX+'然后今天准备开始新的测试'


class StableClauseTests(unittest.TestCase):
    def partials(self, dispatcher, text=TEXT, seq=1):
        for index, suffix in enumerate(['一', '一些', '一些内容']):
            dispatcher.accept({'type': 'partial', 'seq': seq, 'text': text+suffix, 'emitted_at': index*.35})

    def test_boundary_needs_time_stability_and_complete_source(self):
        self.assertEqual(stable_cut([[0, TEXT], [.35, TEXT+'一'], [.7, TEXT+'一些']], ''), len(PREFIX))
        for text in ['如果昨天我们完成了这个方案然后今天继续测试',
                     '我们需要付出的费用是一万然后今天继续测试',
                     '昨天我告诉大家这个方案是然后今天继续测试',
                     '昨天我们完成了这个方案然后今']:
            with self.subTest(text=text):
                self.assertIsNone(stable_cut([[0, text], [.35, text], [.7, text]], ''))
        self.assertIsNone(stable_cut([[0, TEXT], [.1, TEXT], [.2, TEXT]], ''))
        self.assertIsNone(stable_cut([[0, TEXT], [.35, '昨天我们没完成这个方案然后今天继续'], [.7, TEXT]], ''))

    def test_early_translation_does_not_hold_source_and_final_has_exact_coverage(self):
        entered, release = threading.Event(), threading.Event()
        rows, requests = [], []
        def translate(url, event, **kwargs):
            requests.append(dict(event))
            entered.set()
            self.assertTrue(release.wait(3))
            return 'We completed the plan.' if len(requests) == 1 else 'Then we start testing today.', .1
        with tempfile.TemporaryDirectory() as directory, patch('subtitle_bridge.translate_recognized_event', side_effect=translate), \
                patch('subtitle_bridge.request_json', return_value={'id': 'job'}) as speech:
            dispatcher = StableClauseDispatcher(rows.append, 'session', 'translator', 'speech',
                spool_path=Path(directory)/'queue.sqlite3')
            try:
                self.partials(dispatcher)
                self.assertTrue(entered.wait(3))
                self.assertFalse(rows[-1]['final'])
                dispatcher.accept({'type': 'final', 'seq': 1, 'text': TEXT, 'emitted_at': 2})
                dispatcher.accept({'type': 'partial', 'seq': 2, 'text': '后面的字幕'})
                self.assertEqual(rows[-1]['zh'], '后面的字幕')
            finally:
                release.set()
                self.assertTrue(dispatcher.close())
        self.assertEqual([r['text'] for r in requests], [PREFIX, TEXT[len(PREFIX):]])
        first = [r for r in rows if r['utterance_id'] == 'session:1']
        final = first[-1]
        self.assertEqual(final['zh'], TEXT)
        self.assertEqual(final['translation_status'], 'completed')
        self.assertEqual(''.join(p['source_text'] for p in final['translation_segments']), TEXT)
        self.assertEqual(requests[1]['translation_context'][0]['source'], PREFIX)
        self.assertEqual([c.args[1]['utterance_id'] for c in speech.call_args_list], ['session:1/part/0', 'session:1/part/1'])
        revisions = [r['revision'] for r in first]
        self.assertEqual(revisions, sorted(set(revisions)))

    def test_previous_utterance_cannot_supply_context_to_new_source(self):
        requests=[]
        def translate(url,event,**kwargs):
            requests.append(dict(event))
            return 'Translated source.',.1
        with patch('subtitle_bridge.translate_recognized_event',side_effect=translate):
            dispatcher=StableClauseDispatcher(lambda r:None,'session','translator')
            dispatcher.accept({'type':'final','seq':1,'text':'要付10000元。'})
            dispatcher.accept({'type':'final','seq':2,'text':'另一件事情。'})
            self.assertTrue(dispatcher.close())
        self.assertEqual(requests[1]['translation_context'],[])

    def test_revised_committed_prefix_waits_for_final_and_speaks_correction(self):
        rows, requests = [], []
        def translate(url, event, **kwargs):
            requests.append(dict(event))
            return 'Translated source.', .1
        with patch('subtitle_bridge.translate_recognized_event', side_effect=translate), \
                patch('subtitle_bridge.request_json', return_value={'id': 'job'}) as speech:
            dispatcher = StableClauseDispatcher(rows.append, 'session', 'translator', 'speech')
            self.partials(dispatcher)
            revised = TEXT.replace('完成了', '没有完成')
            self.partials(dispatcher, revised)
            dispatcher.accept({'type': 'final', 'seq': 1, 'text': revised, 'emitted_at': 4})
            self.assertTrue(dispatcher.close())
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[-1]['text'], revised)
        self.assertTrue(requests[-1]['source_correction'])
        self.assertTrue(speech.call_args_list[-1].args[1]['text'].startswith('Correction. '))
        self.assertTrue(rows[-1]['source_revision_warning'])
        self.assertEqual(rows[-1]['en'], 'Correction. Translated source.')

    def test_pending_clause_recovers_with_parent_caption_and_speech_key(self):
        entered, release = threading.Event(), threading.Event()
        rows = []
        def translate(*args, **kwargs):
            entered.set()
            release.wait(3)
            return 'Translated source.', .1
        with tempfile.TemporaryDirectory() as directory, patch('subtitle_bridge.translate_recognized_event', side_effect=translate):
            path = Path(directory)/'queue.sqlite3'
            dispatcher = StableClauseDispatcher(rows.append, 'original', 'translator', spool_path=path)
            self.partials(dispatcher)
            self.assertTrue(entered.wait(3))
            dispatcher.accept({'type': 'final', 'seq': 1, 'text': TEXT, 'emitted_at': 2})
            self.assertFalse(dispatcher.close(0))
            count = len(rows)
            release.set()
            dispatcher.worker.join(3)
            self.assertEqual(len(rows), count)
            # Keep the queued tail, but simulate an interrupted parent-state write.
            with sqlite3.connect(path) as database:
                stored=json.loads(database.execute('SELECT payload FROM clause_records WHERE key=?',('original:1',)).fetchone()[0])
                stored['translation_segments']=stored['translation_segments'][:1]
                database.execute('UPDATE clause_records SET payload=? WHERE key=?',(json.dumps(stored),'original:1'))
            resumed = StableClauseDispatcher(rows.append, 'new', 'translator', spool_path=path)
            self.assertTrue(resumed.close())
        self.assertEqual(rows[-1]['utterance_id'], 'original:1')
        self.assertEqual(rows[-1]['translation_status'], 'completed')
        self.assertEqual(len(rows[-1]['translation_segments']), 2)

    def test_evicted_parents_keep_all_work_and_failed_candidate(self):
        entered, release = threading.Event(), threading.Event()
        rows, calls = [], []
        def translate(url, event, audit=None, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            calls.append(event['text'])
            if event['text'] == '第一句。':
                audit.append({'content': 'Candidate 候选.'})
                raise ValueError('Translation rejected')
            return 'Next sentence.', .1
        with tempfile.TemporaryDirectory() as directory, patch('subtitle_bridge.translate_recognized_event', side_effect=translate), \
                patch('subtitle_bridge.request_json', return_value={'id': 'job'}) as speech:
            dispatcher = StableClauseDispatcher(rows.append, 'session', 'translator', 'speech',
                spool_path=Path(directory)/'queue.sqlite3')
            dispatcher.accept({'type': 'final', 'seq': 1, 'text': '第一句。'})
            self.assertTrue(entered.wait(3))
            for index in range(2, 141):
                dispatcher.accept({'type': 'final', 'seq': index, 'text': '下一句。'})
            self.assertLessEqual(len(dispatcher.latest), 128)
            release.set()
            self.assertTrue(dispatcher.close())
        self.assertEqual(len(calls), 140)
        first = [r for r in rows if r['utterance_id'] == 'session:1'][-1]
        self.assertEqual(first['en'], 'Candidate 候选.')
        self.assertEqual(first['translation_status'], 'failed')
        self.assertTrue(first['speech_degraded'])
        self.assertEqual(speech.call_args_list[0].args[1]['text'], 'Candidate 候选.')

    def test_final_numeric_warning_remains_active_after_early_commit(self):
        rows = []
        with patch('subtitle_bridge.request_json', return_value={'choices': [{'message': {'content': 'A translation.'}, 'finish_reason': 'stop'}]}):
            dispatcher = StableClauseDispatcher(rows.append, 'session', 'translator')
            self.partials(dispatcher)
            dispatcher.accept({'type': 'final', 'seq': 1, 'text': TEXT, 'emitted_at': 3,
                'asr_numeric_audit': {'issues': ['quantity_disagreement']}})
            self.assertTrue(dispatcher.close())
        self.assertEqual(rows[-1]['translation_status'], 'failed')
        self.assertIn('Recognition uncertain', rows[-1]['translation_error'])

    def test_final_equal_to_committed_prefix_does_not_repeat_speech(self):
        rows = []
        with patch('subtitle_bridge.translate_recognized_event', return_value=('Completed plan.', .1)) as translate:
            dispatcher = StableClauseDispatcher(rows.append, 'session', 'translator')
            self.partials(dispatcher)
            dispatcher.accept({'type': 'final', 'seq': 1, 'text': PREFIX, 'emitted_at': 3})
            dispatcher.accept({'type': 'partial', 'seq': 1, 'text': TEXT, 'emitted_at': 4})
            self.assertTrue(dispatcher.close())
        translate.assert_called_once()
        self.assertEqual(rows[-1]['zh'], PREFIX)
        self.assertEqual(rows[-1]['translation_status'], 'completed')
        self.assertEqual(len(rows[-1]['translation_segments']), 1)


if __name__ == '__main__':
    unittest.main()
