import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import wave
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speech_service import ApiError, SpeechService

FAKE = '''import json, sys, time, wave
print('runtime diagnostic before ready')
print(json.dumps({'ready': True}), flush=True)
for line in sys.stdin:
 j=json.loads(line)
 if j['text']=='slow': time.sleep(10)
 if j['text']=='fail': print(json.dumps({'error':'deliberate failure'}),flush=True); continue
 duration=.15 if j['text'].startswith('chunk') else .01
 if j['text'].startswith('chunk'): time.sleep(.08)
 with wave.open(j['output'],'wb') as w:
  w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\\0\\0'*int(duration*24000))
 print(json.dumps({'duration_seconds':duration,'sample_rate':24000}),flush=True)
'''


class SpeechTests(unittest.TestCase):
    def test_invalid_source_timing_is_rejected_before_admission(self):
        for field in ['source_duration_seconds', 'source_emitted_at', 'delivery_created_at']:
            for value in [float('nan'), float('inf'), -1, True, '2', 10**400]:
                with self.subTest(field=field, value=value), self.assertRaises(ApiError) as error:
                    self.submit(**{field: value})
                self.assertEqual(error.exception.code, 400)

    def test_explicit_stop_clears_tempo_history(self):
        self.service.tempo_controller.previous['fixed'] = (1.05, time.time())
        self.service.route('POST', '/api/speech/stop', {})
        self.assertEqual(self.service.tempo_controller.previous, {})

    def test_stop_closes_blocked_input_and_reaps_process(self):
        import subprocess
        child = subprocess.Popen([sys.executable, '-c',
            'import signal,sys;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
            'print("ready",flush=True);sys.stdin.buffer.read()'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self.assertEqual(child.stdout.readline(), b'ready\n')
            began = time.monotonic()
            SpeechService.kill(child)
            self.assertLess(time.monotonic()-began, .6)
            self.assertIsNotNone(child.poll())
            self.assertTrue(child.stdin.closed)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        worker = root/'fake.py'
        worker.write_text(FAKE)
        self.service = SpeechService(root, [sys.executable, str(worker)], timeout=.5, history_limit=8)
        self.service.voices['test'] = {'id':'test','revision':'fixed','transcript':'',
            'engine':'pocket','name':'Test','status':'needs_preview'}
        (root/'voices/test.wav').write_bytes(b'reference')
        original = __import__('subprocess').Popen
        def launch(command, **kwargs):
            if command[0] == 'aplay':
                command = [sys.executable, '-c', 'pass']
            return original(command, **kwargs)
        self.player_patch = patch('speech_service.subprocess.Popen', side_effect=launch)
        self.player_patch.start()
        self.service.device = 'null'
    def tearDown(self):
        self.service.close()
        self.player_patch.stop()
        self.temp.cleanup()
    def submit(self, text='Hello world.', **kwargs):
        return self.service.submit({'text':text, 'voice_id':'test', **kwargs})
    def wait(self, job_id):
        end=time.monotonic()+3
        while time.monotonic()<end:
            job=self.service.route('GET',f'/api/speech/jobs/{job_id}',{})
            if job['status'] in {'completed','failed','cancelled','expired'}: return job
            time.sleep(.01)
        self.fail('job did not finish')
    def test_deferred_text_counts_toward_projected_first_play(self):
        # Deferred text reserves no audio capacity but still precedes the new
        # sentence in FIFO playback. Reject before doing avoidable synthesis.
        with self.service.lock:
            for i in range(3):
                self.service.jobs[str(i)] = {'id':str(i),'status':'queued',
                    'playback':True,'admission_pending':True,
                    'estimated_duration_seconds':10,'played_duration_seconds':0}
            self.assertEqual(self.service.audio_backlog_seconds(),0)
            self.assertEqual(self.service.audio_backlog_seconds(include_deferred=True),30)
            with self.assertRaises(ApiError) as error:
                self.submit(live=True,playback=True)
            self.assertEqual(error.exception.code,429)
            self.assertIn('first playback',error.exception.message)
            self.assertEqual(len(self.service.jobs),3)
            self.service.jobs.clear()

    def test_atomic_result_and_reference_snapshot(self):
        job=self.submit()
        self.assertEqual(job['voice_revision'],'fixed')
        self.assertEqual(self.wait(job['id'])['status'],'completed')
        data=self.service.route('GET',job['result_url'],{})
        self.assertEqual(data[:4],b'RIFF')
        self.assertFalse(list((self.service.root/'results').glob('*.tmp')))
    def test_deduplicate_before_queue_admission(self):
        first=self.submit(utterance_id='event:1')
        self.assertEqual(first['id'],self.submit(utterance_id='event:1')['id'])
    def test_outbox_retries_lost_response_without_creating_a_second_job(self):
        from subtitle_bridge import CaptionDispatcher
        self.service.route('POST', '/api/speech/session', {'enabled': True, 'voice_id': 'test'})
        accepted, rows = [], []
        def request(url, payload, **kwargs):
            result = self.service.route('POST', '/api/speech/events', payload)
            accepted.append(result['id'])
            if len(accepted) == 1:
                raise TimeoutError('Admission succeeded but response was lost')
            return result
        with patch('subtitle_bridge.translate_recognized_event', return_value=('Hello.', .1)), \
                patch('subtitle_bridge.request_json', side_effect=request), \
                patch('speech_outbox.RETRY_DELAY_SECONDS', 0):
            dispatcher = CaptionDispatcher(rows.append, 'session', 'translator', 'speech',
                spool_path=Path(self.temp.name)/'captions.sqlite3')
            dispatcher.accept({'type': 'final', 'seq': 1, 'text': '你好', 'emitted_at': time.time()})
            self.assertTrue(dispatcher.close())
        self.assertEqual(len(accepted), 2)
        self.assertEqual(accepted[0], accepted[1])
        self.assertEqual(len([j for j in self.service.jobs.values() if j['utterance_id'] == 'session:1']), 1)
        self.assertEqual(rows[-1]['speech_submission_status'], 'submitted')

    def test_stop_rejects_delayed_delivery_after_session_is_enabled_again(self):
        before_stop = time.time()-1
        self.service.route('POST', '/api/speech/stop', {})
        self.service.route('POST', '/api/speech/session', {'enabled': True, 'voice_id': 'test'})
        for timing in ({'source_emitted_at': before_stop}, {'delivery_created_at': before_stop}):
            with self.subTest(timing=timing), self.assertRaises(ApiError) as error:
                self.service.route('POST', '/api/speech/events', {
                    'text': 'Delayed.', 'utterance_id': 'old-source', **timing})
            self.assertEqual(error.exception.code, 409)
        job = self.service.route('POST', '/api/speech/events', {
            'text': 'New source.', 'utterance_id': 'new-source', 'source_emitted_at': time.time()})
        self.assertEqual(job['status'], 'queued')
        self.assertEqual(self.service.status()['last_stop_at'], self.service.last_stop_at)

    def test_stop_cutoff_precedes_existing_and_archived_deduplication(self):
        emitted = time.time()
        job = self.submit(live=True, utterance_id='stop-before-dedup', source_emitted_at=emitted)
        self.service.route('POST', '/api/speech/stop', {})
        self.service.route('POST', '/api/speech/session', {'enabled': True, 'voice_id': 'test'})
        request = {'text': 'Hello world.', 'utterance_id': 'stop-before-dedup',
                   'source_emitted_at': emitted, 'delivery_created_at': time.time()}
        with self.assertRaises(ApiError) as error:
            self.service.route('POST', '/api/speech/events', request)
        self.assertEqual(error.exception.code, 409)
        self.service.history_limit = 0
        deadline = time.monotonic()+3
        while job['id'] in self.service.jobs and time.monotonic() < deadline:
            self.service.prune()
            time.sleep(.01)
        self.assertNotIn(job['id'], self.service.jobs)
        with self.assertRaises(ApiError) as error:
            self.service.route('POST', '/api/speech/events', request)
        self.assertEqual(error.exception.code, 409)

    def test_stop_cutoff_survives_service_restart(self):
        before_stop = time.time()-1
        voice = dict(self.service.voices['test'])
        self.service.route('POST', '/api/speech/stop', {})
        stopped_at, command = self.service.last_stop_at, self.service.command
        self.service.close()
        self.service = SpeechService(self.temp.name, command)
        self.service.voices['test'] = voice
        self.service.route('POST', '/api/speech/session', {'enabled': True, 'voice_id': 'test'})
        self.assertEqual(self.service.last_stop_at, stopped_at)
        with self.assertRaises(ApiError) as error:
            self.service.route('POST', '/api/speech/events', {
                'text': 'Delayed.', 'source_emitted_at': before_stop})
        self.assertEqual(error.exception.code, 409)

    def test_invalid_saved_stop_timestamp_fails_before_workers_start(self):
        path = Path(self.temp.name)/'speech-stop.json'
        for value in (True, None, 'yesterday', -1, float('nan'), float('inf'), 10**400, time.time()+60):
            with self.subTest(value=str(value)[:30]):
                path.write_text(json.dumps({'last_stop_at': value}))
                with self.assertRaisesRegex(ValueError, 'Invalid speech stop timestamp'):
                    SpeechService(self.temp.name, self.service.command)

    def test_stop_timestamp_storage_failure_is_queryable(self):
        with patch('speech_service.atomic_json', side_effect=OSError('Disk full')):
            result = self.service.route('POST', '/api/speech/stop', {})
        self.assertGreaterEqual(result['stop_errors'], 1)
        status = self.service.status()
        self.assertTrue(status['storage_failed'])
        self.assertIn('Disk full', status['storage_warning'])
        self.assertFalse(status['session']['enabled'])

    def test_bounded_queue_and_cancel_pending(self):
        first=self.submit('slow')
        other=[self.submit('slow') for _ in range(3)]
        with self.assertRaises(ApiError) as error: self.submit()
        self.assertEqual(error.exception.code,429)
        self.service.cancel(other[-1]['id'])
        self.assertEqual(self.wait(other[-1]['id'])['status'],'cancelled')
        self.service.cancel(first['id'])
        self.assertEqual(self.wait(first['id'])['status'],'cancelled')
    def test_cancel_then_worker_recovers(self):
        job=self.submit('slow')
        time.sleep(.1)
        self.service.cancel(job['id'])
        next_job=self.submit()
        self.assertEqual(self.wait(next_job['id'])['status'],'completed')
    def test_worker_timeout_and_recovery(self):
        self.assertEqual(self.wait(self.submit('slow')['id'])['status'],'failed')
        self.assertEqual(self.wait(self.submit()['id'])['status'],'completed')
    def test_failed_result_unavailable(self):
        job=self.submit('fail')
        self.assertEqual(self.wait(job['id'])['status'],'failed')
        with self.assertRaises(ApiError): self.service.route('GET',job['result_url'],{})
    def test_language_and_profile_rejection(self):
        for args in [{'language':'zh'}, {'text':'你好'}, {'voice_id':'missing'}]:
            with self.assertRaises(ApiError): self.service.submit({'text':'hello','voice_id':'test',**args})
    def test_degraded_candidate_fails_before_queue_admission(self):
        with self.service.lock:
            before = len(self.service.jobs)
            for text in ['This is called 跑分.', 'Rejected but ASCII.']:
                with self.assertRaises(ApiError) as error:
                    self.submit(text, speech_degraded=True)
                self.assertEqual(error.exception.code, 422)
            self.assertEqual(len(self.service.jobs), before)
        with self.assertRaises(ApiError) as error:
            self.submit('This is called 跑分.', speech_degraded='true')
        self.assertEqual(error.exception.code, 400)
    def test_live_expiry_and_reference_protection(self):
        self.submit('slow')
        job=self.submit(live=True)
        with self.service.lock: self.service.jobs[job['id']]['created_at']-=31
        with self.assertRaises(ApiError): self.service.delete_voice('test')
        self.assertEqual(self.wait(job['id'])['status'],'expired')
    def test_stop_disables_session_and_cancels(self):
        self.service.route('POST','/api/speech/session',{'enabled':True,'voice_id':'test'})
        job=self.service.route('POST','/api/speech/events',{'text':'slow','utterance_id':'u1'})
        self.service.route('POST','/api/speech/stop',{})
        self.assertEqual(self.wait(job['id'])['status'],'cancelled')
        self.assertIn('skipped',self.service.route('POST','/api/speech/events',{'text':'hi'}))
    def test_restart_preserves_results_and_deduplication(self):
        job=self.submit(utterance_id='persist:1')
        self.assertEqual(self.wait(job['id'])['status'],'completed')
        command=self.service.command
        self.service.close()
        self.service=SpeechService(self.temp.name,command)
        self.assertEqual(self.service.jobs[job['id']]['status'],'completed')
        self.assertEqual(self.submit(utterance_id='persist:1')['id'],job['id'])

    def test_first_piece_plays_before_generation_finishes(self):
        job = self.submit(' '.join(['chunk']*36), live=True, playback=True)
        result = self.wait(job['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['chunk_count'], 3)
        self.assertEqual(result['played_chunks'], 3)
        self.assertLess(result['playback_started_at'], result['generation_finished_at'])
        self.assertEqual(' '.join(c['text'] for c in result['chunks']), job['text'])
        self.assertEqual([c['index'] for c in result['chunks']], [0,1,2])
        with wave.open(str(self.service.root/'results'/f"{job['id']}.wav")) as audio:
            self.assertEqual(audio.getnframes(), 3*3600)

    def test_live_small_jobs_use_duration_budget(self):
        jobs = [self.submit('chunk', live=True, playback=True) for _ in range(6)]
        self.assertTrue(all(self.wait(j['id'])['status']=='completed' for j in jobs))
        with self.assertRaises(ApiError) as error:
            self.submit(' '.join(['word']*69), live=True, playback=True)
        self.assertEqual(error.exception.code, 429)
        self.assertIn('audio budget', error.exception.message)

    def test_cancel_partial_generation_and_playback(self):
        job = self.submit(' '.join(['chunk']*36), live=True, playback=True)
        deadline = time.monotonic()+2
        while time.monotonic()<deadline:
            with self.service.lock:
                if 'playback_started_at' in self.service.jobs[job['id']]:
                    break
            time.sleep(.01)
        else:
            self.fail('first piece never played')
        self.service.cancel(job['id'])
        self.assertEqual(self.wait(job['id'])['status'], 'cancelled')
        with self.service.lock:
            self.assertFalse(any(p['job_id']==job['id'] for p in self.service.play_pending))
        self.assertEqual(self.wait(self.submit()['id'])['status'], 'completed')

    def test_deferred_text_waits_for_capacity_without_synthesis(self):
        with patch.object(self.service, 'audio_backlog_seconds', return_value=24) as backlog:
            job=self.submit('Hello world.',live=True,playback=True)
            time.sleep(.1)
            self.assertTrue(job['admission_pending'])
            self.assertFalse(self.service.jobs[job['id']]['chunks'])
            self.assertIsNone(self.service.child)
            backlog.return_value=0
            self.assertEqual(self.wait(job['id'])['status'],'completed')
            self.assertTrue(self.service.jobs[job['id']]['was_deferred'])
            self.assertFalse(self.service.jobs[job['id']]['admission_pending'])

    def test_deferred_queue_bounded_and_cancellable(self):
        with patch.object(self.service, 'audio_backlog_seconds', return_value=24):
            jobs=[self.submit('Hello world.',live=True,playback=True) for _ in range(4)]
            with self.assertRaises(ApiError) as error:self.submit('Hello world.',live=True,playback=True)
            self.assertEqual(error.exception.code,429)
            self.service.cancel(jobs[0]['id'])
            self.assertEqual(self.wait(jobs[0]['id'])['status'],'cancelled')
            self.assertTrue(self.submit('Hello world.',live=True,playback=True)['admission_pending'])

    def test_deferred_expiry_preserves_original_deadline(self):
        with patch.object(self.service, 'audio_backlog_seconds', return_value=24):
            job=self.submit('Hello world.',live=True,playback=True)
            with self.service.lock:self.service.jobs[job['id']]['created_at']-=31
            self.assertEqual(self.wait(job['id'])['status'],'expired')
            self.assertFalse(self.service.jobs[job['id']]['chunks'])

    def test_download_deadline_ends_when_first_piece_is_ready(self):
        job=self.submit(' '.join(['chunk']*36),live=True,playback=False)
        end=time.monotonic()+2
        while time.monotonic()<end:
            with self.service.lock:
                if self.service.jobs[job['id']]['chunks']:
                    self.service.jobs[job['id']]['created_at']-=31
                    break
            time.sleep(.005)
        else:self.fail('no first piece')
        result=self.wait(job['id'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['generated_chunks'],3)


    def test_preload_ready_without_synthesizing_or_creating_jobs(self):
        command=self.service.command
        self.service.close()
        self.service=SpeechService(self.temp.name,command,preload=True)
        end=time.monotonic()+2
        while time.monotonic()<end and not self.service.status()['runtime']:time.sleep(.01)
        self.assertTrue(self.service.status()['runtime']['ready'])
        self.assertIsNone(self.service.status()['warmup_error'])
        self.assertFalse(self.service.jobs)
        self.assertFalse(list((self.service.root/'results').glob('*.wav')))
        child=self.service.child
        self.service.close()
        self.assertIsNotNone(child.poll())

class CoverageSpeechTests(unittest.TestCase):
    submit = SpeechTests.submit
    wait = SpeechTests.wait
    tearDown = SpeechTests.tearDown

    def setUp(self):
        SpeechTests.setUp(self)
        command, voices = self.service.command, self.service.voices
        self.service.close()
        self.service = SpeechService(self.temp.name, command, timeout=.5,
                                     history_limit=8, delivery_policy='coverage', playback_device='null')
        self.service.voices = voices
        (self.service.root/'voices/test.json').write_text(json.dumps(voices['test']))

    def test_disk_backlog_exceeds_old_limits_without_retaining_all_payloads(self):
        with self.service.lock:
            jobs = [self.submit(utterance_id=f'source:{i}', live=True, playback=True) for i in range(200)]
            status = self.service.status()
            self.assertEqual(status['unfinished_jobs'], 200)
            self.assertEqual(status['pending_jobs'], 200)
            self.assertLessEqual(len(status['jobs']), 8)
            self.assertLessEqual(len(self.service.jobs.cache), 8)
            self.assertEqual(self.submit(utterance_id='source:0')['id'], jobs[0]['id'])
            with self.assertRaises(ApiError):
                self.service.delete_voice('test')
            self.assertFalse(self.service.expire_before_first_audio({**jobs[0], 'created_at': 0}))

    def test_old_tasks_play_in_order_without_deadline_expiry(self):
        with self.service.lock:
            jobs = [self.submit(live=True, playback=True) for _ in range(6)]
            for job in jobs:
                self.service.jobs[job['id']]['created_at'] -= 600
                self.service.save_job(self.service.jobs[job['id']])
        results = [self.wait(j['id']) for j in jobs]
        self.assertTrue(all(j['status'] == 'completed' for j in results))
        self.assertEqual([j['playback_started_at'] for j in results],
                         sorted(j['playback_started_at'] for j in results))

    def test_shutdown_and_restart_keep_waiting_tasks_and_deduplication(self):
        command = self.service.command
        self.service.close()
        with patch.object(SpeechService, 'run'):
            self.service = SpeechService(self.temp.name, command, delivery_policy='coverage')
        jobs = [self.submit(utterance_id=f'persist:{i}', live=True, playback=True) for i in range(6)]
        self.service.close()
        self.service = SpeechService(self.temp.name, command, delivery_policy='coverage', playback_device='null')
        for job in jobs:
            self.assertEqual(self.wait(job['id'])['status'], 'completed')
            self.assertEqual(self.submit(utterance_id=job['utterance_id'])['id'], job['id'])

    def test_explicit_stop_cancels_disk_backlog(self):
        with self.service.lock:
            jobs = [self.submit(live=True, playback=True) for _ in range(12)]
            self.service.route('POST', '/api/speech/stop', {})
            self.assertEqual(self.service.jobs.unfinished(), 0)
            self.assertEqual(len(self.service.pending), 0)
            self.assertTrue(all(j['status'] == 'cancelled' for j in self.service.status()['jobs']))
            self.assertEqual(len(self.service.jobs), 8)

    def test_two_services_cannot_consume_the_same_disk_queue(self):
        with self.assertRaisesRegex(RuntimeError, 'owns this disk queue'):
            SpeechService(self.temp.name, self.service.command, delivery_policy='coverage')

    def test_restart_reports_interrupted_playback_without_replaying_it(self):
        command = self.service.command
        self.service.close()
        with patch.object(SpeechService, 'run'):
            self.service = SpeechService(self.temp.name, command, delivery_policy='coverage')
        job = self.submit(live=True, playback=True)
        self.service.close()
        job.update(status='playing', playback_started_at=time.time())
        with sqlite3.connect(str(Path(self.temp.name)/'speech-jobs.sqlite3')) as database:
            database.execute('UPDATE jobs SET payload=?, status=?, pending=0 WHERE id=?',
                             (json.dumps(job), 'playing', job['id']))
        self.service = SpeechService(self.temp.name, command, delivery_policy='coverage')
        recovered = self.service.route('GET', f"/api/speech/jobs/{job['id']}", {})
        self.assertEqual(recovered['status'], 'failed')
        self.assertIn('Playback was interrupted', recovered['error'])
        self.assertIsNone(self.service.child)


if __name__=='__main__': unittest.main()
