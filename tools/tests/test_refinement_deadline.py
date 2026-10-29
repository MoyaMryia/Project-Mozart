import pathlib
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from subtitle_bridge import CaptionDispatcher, source_caption, caption_updates
from durable_jobs import DiskFifo


class RefinementDeadlineTests(unittest.TestCase):
    def scenario(self, queued_for, *, formatting_time=0, completed_refinement=False):
        clock = [100.0]
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        rows, waits, translated = [], [], []

        def translate(url, event, **kwargs):
            translated.append((event['seq'], event['text']))
            if event['seq'] == 1:
                entered.set()
                self.assertTrue(release.wait(3))
            return 'Chinese and English.', .1

        def publish(row):
            rows.append(row)
            if row.get('seq') == 2 and row.get('translation_status') == 'completed':
                done.set()

        def format_source(event, *args):
            if event['seq'] == 2:
                clock[0] += formatting_time
            return source_caption(event, *args)

        def wait(seconds):
            waits.append(seconds)
            clock[0] += seconds

        with patch('subtitle_bridge.time.monotonic', side_effect=lambda: clock[0]), \
                patch('subtitle_bridge.refinement_wait', return_value=1.0), \
                patch('subtitle_bridge.source_caption', side_effect=format_source), \
                patch('subtitle_bridge.translate_recognized_event', side_effect=translate):
            dispatcher = CaptionDispatcher(publish, 'session', 'translator', refine=True)
            with patch.object(dispatcher.refined_ready, 'wait', side_effect=wait):
                try:
                    dispatcher.accept({'type': 'final', 'seq': 1, 'text': '上一句',
                                       'refinement_status': 'disabled'})
                    self.assertTrue(entered.wait(3))
                    dispatcher.accept({'type': 'final', 'seq': 2, 'text': '中东英文',
                                       'refinement_status': 'pending'})
                    clock[0] += queued_for
                    if completed_refinement:
                        dispatcher.accept({'type': 'refined', 'seq': 2, 'text': '中东英文',
                            'refined_text': '中英文。', 'refinement_status': 'completed'})
                    release.set()
                    self.assertTrue(done.wait(3))
                finally:
                    release.set()
                    self.assertTrue(dispatcher.close(3))
        return rows[-1], waits, translated

    def test_queue_time_consumes_the_refinement_window(self):
        row, waits, _ = self.scenario(.4)
        self.assertEqual(len(waits), 1)
        self.assertAlmostEqual(waits[0], .6)
        self.assertEqual(row['translation_queue_wait_ms'], 400)
        self.assertEqual(row['translation_refinement_wait_ms'], 600)
        self.assertEqual(row['translation_source_reason'], 'refinement_timeout')

    def test_expired_queued_job_does_not_start_a_new_wait(self):
        row, waits, translated = self.scenario(2)
        self.assertEqual(waits, [])
        self.assertEqual(row['translation_queue_wait_ms'], 2000)
        self.assertEqual(row['translation_refinement_wait_ms'], 0)
        self.assertEqual(translated[-1], (2, '中东英文'))

    def test_source_formatting_also_consumes_the_refinement_window(self):
        row, waits, _ = self.scenario(.5, formatting_time=.25)
        self.assertEqual(len(waits), 1)
        self.assertAlmostEqual(waits[0], .25)
        self.assertEqual(row['translation_queue_wait_ms'], 500)
        self.assertEqual(row['translation_refinement_wait_ms'], 250)

    def test_ready_refinement_is_used_even_when_the_window_expired(self):
        row, waits, translated = self.scenario(2, completed_refinement=True)
        self.assertEqual(waits, [])
        self.assertEqual(row['translation_source_reason'], 'refined')
        self.assertEqual(translated[-1], (2, '中英文。'))


class RestoredRefinementDeadlineTests(unittest.TestCase):
    def restored_case(self, *, include_owner):
        rows, seen = [], []
        start, original_done, live_done = (threading.Event() for _ in range(3))
        process_pending = CaptionDispatcher.process_pending

        def delayed_start(dispatcher):
            self.assertTrue(start.wait(3))
            return process_pending(dispatcher)

        def publish(row):
            rows.append(row)
            if row.get('translation_status') == 'completed':
                (original_done if row['utterance_id'] == 'original:2' else live_done).set()

        def translate(url, event, **kwargs):
            seen.append(event['text'])
            return 'Chinese and English.', .1

        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'queue.sqlite3'
            event = {'type': 'final', 'seq': 2, 'text': '中东英文', 'refinement_status': 'pending'}
            disk = DiskFifo(path)
            item = {'event': event, 'session_id': 'original',
                    'record': source_caption(event, 2, 'original')}
            if include_owner:
                item.update(queue_owner='previous-instance', enqueued_monotonic=0.0)
            disk.put_nowait(item)
            disk.close()
            with patch.object(CaptionDispatcher, 'process_pending', autospec=True, side_effect=delayed_start), \
                    patch('subtitle_bridge.translate_recognized_event', side_effect=translate):
                dispatcher = CaptionDispatcher(publish, 'live', 'translator', spool_path=path, refine=True)
                with patch.object(dispatcher.refined_ready, 'wait', wraps=dispatcher.refined_ready.wait) as wait:
                    try:
                        dispatcher.accept({'type': 'refined', 'seq': 2, 'text': '中东英文',
                            'refined_text': '中英文。', 'refinement_status': 'completed'})
                        start.set()
                        self.assertTrue(original_done.wait(3))
                        dispatcher.accept(event)
                        self.assertTrue(live_done.wait(3))
                    finally:
                        start.set()
                        self.assertTrue(dispatcher.close(3))
        original = next(row for row in rows if row['utterance_id'] == 'original:2'
                        and row['translation_status'] == 'completed')
        self.assertEqual(original['translation_source_reason'], 'refinement_recovered_unavailable')
        self.assertIsNone(original['translation_queue_wait_ms'])
        self.assertEqual(seen, ['中东英文', '中英文。'])
        wait.assert_not_called()

    def test_old_disk_job_does_not_wait_or_consume_a_live_session_candidate(self):
        for include_owner in (False, True):
            with self.subTest(include_owner=include_owner):
                self.restored_case(include_owner=include_owner)

    def test_restored_inline_evidence_remains_usable_without_a_new_deadline(self):
        dispatcher = CaptionDispatcher(lambda row: None, 'session', 'translator', refine=True)
        try:
            diagnostics = {}
            text = dispatcher.await_refinement({'seq': 1, 'text': '中东英文',
                'refined_text': '中英文。', 'refinement_status': 'completed'},
                deadline=None, diagnostics=diagnostics)
        finally:
            self.assertTrue(dispatcher.close(3))
        self.assertEqual(text, '中英文。')
        self.assertEqual(diagnostics['translation_source_reason'], 'refined')

    def test_expired_ready_candidate_still_enforces_recognition_audits(self):
        dispatcher = CaptionDispatcher(lambda row: None, 'session', 'translator', refine=True)
        event = {'seq': 1, 'text': '我们收到了七千元'}
        try:
            dispatcher.accept({**event, 'type': 'refined', 'refined_text': '我们收到了7000元。',
                'refinement_status': 'completed',
                'asr_numeric_audit': {'issues': ['itn_quantity_disagreement']}})
            diagnostics = {}
            text = dispatcher.await_refinement(event, deadline=0, diagnostics=diagnostics)
            with patch('subtitle_bridge.request_json') as request:
                rows = list(caption_updates({**event, **diagnostics}, 1, 'session',
                                           'translator', 'speech', translate_text=text))
            request.assert_not_called()
        finally:
            self.assertTrue(dispatcher.close(3))
        self.assertEqual(diagnostics['translation_source_reason'], 'refined')
        self.assertEqual(rows[-1]['translation_status'], 'failed')
        self.assertEqual(rows[-1]['speech_skipped_reason'], 'asr_refinement_uncertainty')


if __name__ == '__main__':
    unittest.main()
