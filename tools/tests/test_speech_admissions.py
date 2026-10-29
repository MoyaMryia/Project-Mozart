import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speech_admissions import AdmissionLedger
from speech_service import ApiError, SpeechService

FAKE = '''import json, sys, wave
print(json.dumps({'ready': True}), flush=True)
for line in sys.stdin:
 j=json.loads(line)
 duration=.01
 with wave.open(j['output'],'wb') as w:
  w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\\0\\0'*int(duration*24000))
 print(json.dumps({'duration_seconds':duration,'sample_rate':24000}),flush=True)
'''

VOICE = {'id': 'test', 'revision': 'fixed', 'transcript': '',
         'engine': 'pocket', 'name': 'Test', 'status': 'needs_preview'}


class AdmissionLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'speech-admissions.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_survives_reopen_and_archive(self):
        job = {'id': 'job-1', 'utterance_id': 'source:1', 'status': 'queued'}
        ledger = AdmissionLedger(self.path)
        ledger.record(job)
        ledger.close()
        ledger = AdmissionLedger(self.path)
        found = ledger.find('source:1')
        self.assertEqual(found['job']['id'], 'job-1')
        self.assertFalse(found['archived'])
        ledger.archive({**job, 'status': 'completed'})
        ledger.close()
        ledger = AdmissionLedger(self.path)
        found = ledger.find('source:1')
        self.assertTrue(found['archived'])
        self.assertEqual(found['job']['status'], 'completed')
        ledger.close()

    def test_only_one_service_can_own_the_admission_ledger(self):
        ledger = AdmissionLedger(self.path)
        try:
            with self.assertRaisesRegex(RuntimeError, 'Another process owns'):
                AdmissionLedger(self.path)
        finally:
            ledger.close()


class AdmissionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        worker = self.root/'fake.py'
        worker.write_text(FAKE)
        self.command = [sys.executable, str(worker)]
        (self.root/'voices').mkdir(exist_ok=True)
        (self.root/'voices/test.json').write_text(json.dumps(VOICE))
        original = __import__('subprocess').Popen

        def launch(command, **kwargs):
            if command[0] == 'aplay':
                command = [sys.executable, '-c', 'pass']
            return original(command, **kwargs)
        self.player_patch = patch('speech_service.subprocess.Popen', side_effect=launch)
        self.player_patch.start()
        self.service = None

    def tearDown(self):
        if self.service is not None:
            self.service.close()
        self.player_patch.stop()
        self.temp.cleanup()

    def start(self, policy='realtime'):
        if self.service is not None:
            self.service.close()
        self.service = SpeechService(self.root, self.command, timeout=.5,
                                     history_limit=8, delivery_policy=policy,
                                     playback_device='null')
        return self.service

    def submit(self, text='Hello world.', **kwargs):
        return self.service.submit({'text': text, 'voice_id': 'test', **kwargs})

    def wait(self, job_id):
        end = time.monotonic()+3
        while time.monotonic() < end:
            job = self.service.route('GET', f'/api/speech/jobs/{job_id}', {})
            if job['status'] in {'completed', 'failed', 'cancelled', 'expired'}:
                return job
            time.sleep(.01)
        self.fail('job did not finish')

    def test_prune_retry_returns_archived_snapshot_for_both_policies(self):
        for policy in ('realtime', 'coverage'):
            with self.subTest(policy=policy):
                self.start(policy)
                key = f'prune:{policy}'
                job = self.submit(utterance_id=key)
                self.assertEqual(self.wait(job['id'])['status'], 'completed')
                current = self.submit(utterance_id=key)
                self.assertEqual(current['id'], job['id'])
                self.assertEqual(current['status'], 'completed')
                self.assertFalse(current.get('archived', False))
                self.service.history_limit = 0
                self.service.prune()
                self.assertNotIn(job['id'], self.service.jobs)
                archived = self.submit(utterance_id=key)
                self.assertEqual(archived['id'], job['id'])
                self.assertEqual(archived['status'], 'completed')
                self.assertTrue(archived['archived'])
                self.assertFalse(archived['result_available'])
                self.assertNotIn('result_url', archived)
                self.assertNotIn(job['id'], self.service.jobs)

    def test_restart_prefers_live_job_then_returns_archived_snapshot(self):
        for policy in ('realtime', 'coverage'):
            with self.subTest(policy=policy):
                self.start(policy)
                key = f'restart:{policy}'
                job = self.submit(utterance_id=key)
                self.assertEqual(self.wait(job['id'])['status'], 'completed')
                self.start(policy)
                current = self.submit(utterance_id=key)
                self.assertEqual(current['id'], job['id'])
                self.assertEqual(current['status'], 'completed')
                self.assertFalse(current.get('archived', False))
                self.service.history_limit = 0
                self.service.prune()
                self.assertNotIn(job['id'], self.service.jobs)
                self.start(policy)
                archived = self.submit(utterance_id=key)
                self.assertEqual(archived['id'], job['id'])
                self.assertTrue(archived['archived'])
                self.assertFalse(archived['result_available'])
                self.assertNotIn(job['id'], self.service.jobs)

    def test_live_job_wins_after_key_index_eviction(self):
        self.start('realtime')
        job = self.submit(utterance_id='evict:1')
        self.assertEqual(self.wait(job['id'])['status'], 'completed')
        with self.service.lock:
            self.service.keys.clear()
        current = self.submit(utterance_id='evict:1')
        self.assertEqual(current['id'], job['id'])
        self.assertEqual(current['status'], 'completed')
        self.assertFalse(current.get('archived', False))

    def test_recovered_job_without_ledger_entry_is_deduplicated(self):
        for policy in ('realtime', 'coverage'):
            with self.subTest(policy=policy):
                self.start(policy)
                key = f'legacy:{policy}'
                job = self.submit(utterance_id=key)
                self.assertEqual(self.wait(job['id'])['status'], 'completed')
                with self.service.admissions.lock, self.service.admissions.database:
                    self.service.admissions.database.execute('DELETE FROM admissions')
                self.start(policy)
                current = self.submit(utterance_id=key)
                self.assertEqual(current['id'], job['id'])
                self.assertEqual(current['status'], 'completed')
                self.assertFalse(current.get('archived', False))

    def test_legacy_job_gets_a_deduplication_record_before_pruning(self):
        self.start('coverage')
        job = self.submit(utterance_id='legacy-prune:1')
        self.assertEqual(self.wait(job['id'])['status'], 'completed')
        with self.service.admissions.lock, self.service.admissions.database:
            self.service.admissions.database.execute('DELETE FROM admissions')
        self.service.history_limit = 0
        self.service.prune()
        archived = self.submit(utterance_id='legacy-prune:1')
        self.assertEqual(archived['id'], job['id'])
        self.assertTrue(archived['archived'])

    def test_ledger_write_failure_stops_admission_and_is_queryable(self):
        self.start('realtime')
        with patch.object(self.service.admissions, 'record',
                          side_effect=sqlite3.OperationalError('ledger full')):
            with self.assertRaises(ApiError) as error:
                self.submit(utterance_id='ledger:failure')
        self.assertEqual(error.exception.code, 507)
        status = self.service.status()
        self.assertTrue(status['storage_failed'])
        self.assertIn('ledger full', status['storage_warning'])
        with self.assertRaises(ApiError) as error:
            self.submit(text='Next request.', utterance_id='ledger:next')
        self.assertEqual(error.exception.code, 503)


if __name__ == '__main__':
    unittest.main()
