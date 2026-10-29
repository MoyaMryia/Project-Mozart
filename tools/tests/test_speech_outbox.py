import io
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from speech_outbox import SpeechOutbox, MAX_ATTEMPTS
from subtitle_bridge import CaptionDispatcher
from stable_clauses import StableClauseDispatcher


def body(key='session:1'):
    return {'text': 'Hello.', 'source_text': '你好', 'utterance_id': key}


def context(key='session:1'):
    return {'key': key, 'record': {'utterance_id': key, 'en': 'Hello.', 'revision': 1}}


class SpeechOutboxTests(unittest.TestCase):
    def deliver(self, request):
        rows = []
        def publish(ctx, changes):
            rows.append(dict(changes))
            return True
        outbox = SpeechOutbox(None, request, publish)
        outbox.enqueue(body(), context())
        outbox.start()
        with patch('speech_outbox.RETRY_DELAY_SECONDS', 0):
            outbox.close()
        return rows

    def test_network_failure_retries_and_keeps_the_original_key(self):
        calls = []
        def request(payload):
            calls.append(dict(payload))
            if len(calls) == 1:
                raise TimeoutError('Response was lost')
            return {'id': 'job'}
        rows = self.deliver(request)
        self.assertEqual(calls, [body(), body()])
        retrying = [r for r in rows if r['speech_submission_status'] == 'retrying']
        self.assertTrue(retrying)
        self.assertTrue(all(r['speech_error'] == '' and r['speech_last_error'] for r in retrying))
        self.assertEqual(rows[-1]['speech_submission_status'], 'submitted')
        self.assertEqual(rows[-1]['speech_submission_attempts'], 2)
        self.assertEqual(rows[-1]['speech_last_error'], 'Response was lost')
        self.assertEqual(rows[-1]['speech_error'], '')

    def test_exhausted_retries_fail_with_a_queryable_reason(self):
        calls = []
        def request(payload):
            calls.append(payload)
            raise ConnectionRefusedError('Speech service unavailable')
        rows = self.deliver(request)
        self.assertEqual(len(calls), MAX_ATTEMPTS)
        self.assertEqual(rows[-1]['speech_submission_status'], 'failed')
        self.assertIn('unavailable', rows[-1]['speech_error'])

    def test_http_client_error_is_not_retried(self):
        calls = []
        def request(payload):
            calls.append(payload)
            raise urllib.error.HTTPError('unused', 422, 'Invalid speech text', {}, io.BytesIO())
        rows = self.deliver(request)
        self.assertEqual(len(calls), 1)
        self.assertEqual(rows[-1]['speech_submission_status'], 'failed')
        self.assertIn('422', rows[-1]['speech_error'])

    def test_http_server_error_is_retried(self):
        calls = []
        def request(payload):
            calls.append(payload)
            if len(calls) == 1:
                raise urllib.error.HTTPError('unused', 503, 'Warming up', {}, io.BytesIO())
            return {'id': 'job'}
        rows = self.deliver(request)
        self.assertEqual(len(calls), 2)
        self.assertEqual(rows[-1]['speech_submission_status'], 'submitted')

    def test_invalid_success_response_fails_instead_of_claiming_submission(self):
        for response in ({}, [], {'id': ''}, {'id': 'job', 'skipped': 'disabled'}):
            with self.subTest(response=response):
                rows = self.deliver(lambda payload: response)
                self.assertEqual(rows[-1]['speech_submission_status'], 'failed')
                self.assertEqual(rows[-1]['speech_submission_attempts'], 1)
                self.assertNotIn('speech', rows[-1])

    def test_disabled_session_is_an_explicit_terminal_skip(self):
        rows = self.deliver(lambda payload: {'skipped': 'speech session disabled'})
        self.assertEqual(rows[-1]['speech_submission_status'], 'skipped')
        self.assertEqual(rows[-1]['speech_skipped_reason'], 'speech session disabled')
        self.assertEqual(rows[-1]['speech_submission_attempts'], 1)

    def test_duplicate_enqueue_is_idempotent_but_conflicting_text_fails(self):
        calls = []
        outbox = SpeechOutbox(None, lambda payload: (calls.append(payload) or {'id': 'job'}),
                              lambda ctx, changes: True)
        outbox.enqueue(body(), context())
        outbox.enqueue(body(), context())
        with self.assertRaisesRegex(ValueError, 'different request'):
            outbox.enqueue({**body(), 'text': 'Different.'}, context())
        outbox.start()
        outbox.close()
        self.assertEqual(calls, [body()])

    def test_aborted_worker_keeps_storage_open_until_coordinator_closes(self):
        outbox = SpeechOutbox(None, lambda payload: {'id': 'job'}, lambda ctx, changes: True)
        outbox.enqueue(body(), context())
        outbox.abort()
        outbox.start()
        outbox.worker.join(3)
        self.assertFalse(outbox.worker.is_alive())
        try:
            saved, changes = outbox.lookup('session:1')
            self.assertEqual(saved['key'], 'session:1')
            self.assertEqual(changes['speech_submission_status'], 'queued')
        finally:
            outbox.close()

    def test_internal_delivery_error_stops_the_worker_and_propagates(self):
        errors = []
        def request(payload):
            raise TypeError('Delivery program error')
        with patch('threading.excepthook', side_effect=lambda args: errors.append(args.exc_value)):
            outbox = SpeechOutbox(None, request, lambda ctx, changes: True)
            outbox.enqueue(body(), context())
            outbox.start()
            with self.assertRaisesRegex(RuntimeError, 'Speech delivery worker failed') as error:
                outbox.close()
        self.assertIsInstance(error.exception.__cause__, TypeError)
        self.assertEqual(len(errors), 1)

    def test_interrupted_last_attempt_remains_unconfirmed_without_exceeding_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'speech.sqlite3'
            outbox = SpeechOutbox(path, lambda payload: {'id': 'job'}, lambda ctx, changes: True)
            outbox.enqueue(body(), context())
            outbox.start()
            outbox.close()
            with sqlite3.connect(path) as database:
                database.execute("UPDATE speech_outbox SET state='sending',attempts=?,result='',notified=0",
                                 (MAX_ATTEMPTS,))
            rows, calls = [], []
            def publish(ctx, changes):
                rows.append(dict(changes))
                return True
            resumed = SpeechOutbox(path, lambda payload: calls.append(payload), publish)
            resumed.start()
            resumed.close()
        self.assertEqual(calls, [])
        self.assertEqual(rows[-1]['speech_submission_status'], 'failed')
        self.assertIn('unconfirmed', rows[-1]['speech_error'])


class IndependentSpeechDeliveryTests(unittest.TestCase):
    def test_blocked_speech_does_not_hold_translation_and_survives_shutdown(self):
        entered, release, queued = (threading.Event() for _ in range(3))
        rows, translated, sent = [], [], []
        def translate(url, event, **kwargs):
            translated.append(event['seq'])
            return 'Hello.', .1
        def publish(row):
            rows.append(row)
            if row.get('seq') == 2 and row.get('speech_submission_status') == 'queued':
                queued.set()
        def request(url, payload, **kwargs):
            sent.append(payload['utterance_id'])
            entered.set()
            self.assertTrue(release.wait(3))
            return {'id': 'first-job'}
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'captions.sqlite3'
            with patch('subtitle_bridge.translate_recognized_event', side_effect=translate), \
                    patch('subtitle_bridge.request_json', side_effect=request):
                dispatcher = CaptionDispatcher(publish, 'original', 'translator', 'speech', spool_path=path)
                try:
                    dispatcher.accept({'type': 'final', 'seq': 1, 'text': '你好'})
                    self.assertTrue(entered.wait(3))
                    dispatcher.accept({'type': 'final', 'seq': 2, 'text': '下一句'})
                    self.assertTrue(queued.wait(3))
                    self.assertEqual(translated, [1, 2])
                    self.assertEqual(sent, ['original:1'])
                    self.assertFalse(dispatcher.close(0))
                    count = len(rows)
                finally:
                    release.set()
                    dispatcher.worker.join(3)
                self.assertFalse(dispatcher.worker.is_alive())
                self.assertEqual(len(rows), count)
            with patch('subtitle_bridge.translate_recognized_event') as translate_again, \
                    patch('subtitle_bridge.request_json', return_value={'id': 'second-job'}) as deliver:
                resumed = CaptionDispatcher(rows.append, 'new', 'translator', 'speech', spool_path=path)
                self.assertTrue(resumed.close())
                translate_again.assert_not_called()
                deliver.assert_called_once()
                self.assertEqual(deliver.call_args.args[1]['utterance_id'], 'original:2')
        self.assertTrue(any(r.get('speech') == 'first-job' for r in rows))
        self.assertTrue(any(r.get('speech') == 'second-job' for r in rows))

    def test_stable_clauses_also_translate_while_speech_is_blocked(self):
        entered, release, queued = (threading.Event() for _ in range(3))
        rows, translated, sent = [], [], []
        def publish(row):
            rows.append(row)
            if row['seq'] == 2 and row.get('speech_submission_status') == 'queued':
                queued.set()
        def translate(url, event, **kwargs):
            translated.append(event['seq'])
            return 'Translated source.', .1
        def request(url, payload, **kwargs):
            sent.append(payload['utterance_id'])
            if len(sent) == 1:
                entered.set()
                self.assertTrue(release.wait(3))
            return {'id': 'job-' + payload['utterance_id']}
        with tempfile.TemporaryDirectory() as directory, \
                patch('subtitle_bridge.translate_recognized_event', side_effect=translate), \
                patch('subtitle_bridge.request_json', side_effect=request):
            dispatcher = StableClauseDispatcher(publish, 'session', 'translator', 'speech',
                spool_path=pathlib.Path(directory)/'clauses.sqlite3')
            try:
                dispatcher.accept({'type': 'final', 'seq': 1, 'text': '完整的第一句。'})
                self.assertTrue(entered.wait(3))
                dispatcher.accept({'type': 'final', 'seq': 2, 'text': '完整的第二句。'})
                self.assertTrue(queued.wait(3))
                self.assertEqual(len(translated), 2)
                self.assertEqual(len(sent), 1)
            finally:
                release.set()
                self.assertTrue(dispatcher.close(3))
        self.assertEqual(len(set(sent)), 2)
        self.assertEqual(rows[-1]['speech_submission_status'], 'submitted')
        self.assertTrue(all(part['speech_submission_status'] == 'submitted'
                            for part in rows[-1]['translation_segments']))

    def test_speech_callback_recovers_an_evicted_caption(self):
        entered, release, queued = (threading.Event() for _ in range(3))
        rows = []
        def publish(row):
            rows.append(row)
            if row['seq'] == 130 and row.get('speech_submission_status') == 'queued':
                queued.set()
        def request(url, payload, **kwargs):
            if payload['utterance_id'] == 'session:1':
                entered.set()
                self.assertTrue(release.wait(10))
            return {'id': 'job-' + payload['utterance_id']}
        with tempfile.TemporaryDirectory() as directory, \
                patch('subtitle_bridge.translate_recognized_event', return_value=('Hello.', .1)), \
                patch('subtitle_bridge.request_json', side_effect=request):
            dispatcher = CaptionDispatcher(publish, 'session', 'translator', 'speech',
                spool_path=pathlib.Path(directory)/'captions.sqlite3')
            try:
                dispatcher.accept({'type': 'final', 'seq': 1, 'text': '第一句。'})
                self.assertTrue(entered.wait(3))
                for seq in range(2, 131):
                    dispatcher.accept({'type': 'final', 'seq': seq, 'text': '后续句子。'})
                self.assertTrue(queued.wait(5))
                self.assertNotIn('session:1', dispatcher.latest)
            finally:
                release.set()
                self.assertTrue(dispatcher.close(5))
        first = next(row for row in rows if row['seq'] == 1 and row.get('speech') == 'job-session:1')
        self.assertEqual(first['zh'], '第一句。')
        self.assertEqual(first['en'], 'Hello.')
        self.assertEqual(first['translation_status'], 'completed')
        self.assertEqual(len({row['speech'] for row in rows if row.get('speech')}), 130)

    def test_enqueue_storage_failure_retains_the_translation_for_restart(self):
        errors, rows = [], []
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)/'captions.sqlite3'
            with patch('subtitle_bridge.translate_recognized_event', return_value=('Hello.', .1)), \
                    patch('threading.excepthook', side_effect=lambda args: errors.append(args.exc_value)):
                dispatcher = CaptionDispatcher(rows.append, 'original', 'translator', 'speech', spool_path=path)
                with patch.object(dispatcher.outbox, 'enqueue', side_effect=OSError('Outbox disk full')):
                    dispatcher.accept({'type': 'final', 'seq': 1, 'text': '你好'})
                    dispatcher.worker.join(3)
                    self.assertFalse(dispatcher.worker.is_alive())
                    with self.assertRaisesRegex(RuntimeError, 'Speech delivery enqueue failed'):
                        dispatcher.close(3)
            self.assertEqual(rows[-1]['translation_status'], 'completed')
            self.assertEqual(rows[-1]['speech_submission_status'], 'failed')
            self.assertIn('Outbox disk full', rows[-1]['speech_error'])
            with patch('subtitle_bridge.translate_recognized_event', return_value=('Hello.', .1)), \
                    patch('subtitle_bridge.request_json', return_value={'id': 'recovered-job'}) as deliver:
                resumed = CaptionDispatcher(rows.append, 'new', 'translator', 'speech', spool_path=path)
                self.assertTrue(resumed.close())
                deliver.assert_called_once()
        self.assertEqual(rows[-1]['speech'], 'recovered-job')
        self.assertTrue(errors)

    def test_delivery_failure_does_not_change_completed_translation_to_failed(self):
        rows = []
        def request(*args, **kwargs):
            raise TimeoutError('Offline speech endpoint')
        with patch('subtitle_bridge.translate_recognized_event', return_value=('Hello.', .1)), \
                patch('subtitle_bridge.request_json', side_effect=request), \
                patch('speech_outbox.RETRY_DELAY_SECONDS', 0):
            dispatcher = CaptionDispatcher(rows.append, 'session', 'translator', 'speech')
            dispatcher.accept({'type': 'final', 'seq': 1, 'text': '你好'})
            self.assertTrue(dispatcher.close())
        self.assertEqual(rows[-1]['translation_status'], 'completed')
        self.assertEqual(rows[-1]['en'], 'Hello.')
        self.assertEqual(rows[-1]['speech_submission_status'], 'failed')
        self.assertIn('Offline speech endpoint', rows[-1]['speech_error'])


if __name__ == '__main__':
    unittest.main()
