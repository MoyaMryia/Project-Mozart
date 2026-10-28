"""用真实子进程检查运行时就绪、准备操作和故障恢复。"""
import fcntl
import io
import json
import errno
import select
import sqlite3
import subprocess
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from durable_jobs import DiskJobs, DiskPending
from speech_service import ApiError, SpeechService, atomic_json
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_speech_service as support


RUNTIME = support.FAKE.replace('import json, sys, time, wave', '''import json, sys, time, wave, os
from pathlib import Path
with Path(__file__).with_suffix('.starts').open('a') as starts:
 starts.write(str(os.getpid())+'\\n')''').replace("{'ready': True}", "{'ready': True, 'worker_pid': os.getpid()}")

CHUNK_WORKER = '''import json,sys,wave
print(json.dumps({'ready': True}), flush=True)
for line in sys.stdin:
 j=json.loads(line)
 with wave.open(j['output'],'wb') as w:
  w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\\x00\\x00'*7200)
 print(json.dumps({'duration_seconds':.3,'sample_rate':24000}),flush=True)
'''


class SpeechRuntimeTests(unittest.TestCase):
    setUp = support.SpeechTests.setUp
    tearDown = support.SpeechTests.tearDown
    submit = support.SpeechTests.submit
    wait = support.SpeechTests.wait

    def replace_worker(self, source=RUNTIME, timeout=1., preload=False, delivery_policy='realtime'):
        voices = self.service.voices
        self.service.close()
        worker = Path(self.temp.name)/'runtime.py'
        worker.write_text(source)
        self.service = SpeechService(self.temp.name, [sys.executable, str(worker)],
                                     timeout=timeout, preload=preload, delivery_policy=delivery_policy)
        self.service.voices = voices
        return worker.with_suffix('.starts')

    def until(self, predicate):
        end = time.monotonic()+3
        while time.monotonic() < end:
            status = self.service.status()
            if predicate(status):
                return status
            time.sleep(.005)
        self.fail('Runtime condition did not complete: '+json.dumps(self.service.status()))

    def prepare_ready(self):
        response = self.service.route('POST', '/api/speech/prepare', {})
        status = self.until(lambda status: status['runtime_ready'])
        return response, status

    def test_prepare_is_serial_idempotent_and_creates_no_speech(self):
        starts = self.replace_worker(RUNTIME.replace("print('runtime diagnostic before ready')",
                                                     "time.sleep(.1)"))
        called_on = []
        ensure = self.service.ensure_child
        def recorded(*args, **kwargs):
            called_on.append(threading.current_thread())
            return ensure(*args, **kwargs)
        with patch.object(self.service, 'ensure_child', side_effect=recorded):
            with self.service.lock:
                responses = [self.service.route('POST', '/api/speech/prepare', {}) for _ in range(10)]
            first = responses[0]['prepare']['id']
            self.assertEqual({r['prepare']['id'] for r in responses}, {first})
            self.until(lambda status: status['runtime_state'] == 'preparing')
            again = self.service.route('POST', '/api/speech/prepare', {})
            self.assertEqual(again['prepare']['id'], first)
            status = self.until(lambda status: status['runtime_ready'])
        pid = self.service.child.pid
        ready = self.service.route('POST', '/api/speech/prepare', {})
        self.assertEqual(ready['prepare']['id'], first)
        self.assertEqual(ready['prepare']['status'], 'ready')
        self.assertEqual(self.service.child.pid, pid)
        self.assertEqual(starts.read_text().splitlines(), [str(pid)])
        self.assertEqual(called_on, [self.service.worker])
        self.assertEqual(status['runtime'], status['last_runtime'])
        self.assertFalse(status['storage_failed'])
        self.assertFalse(status['jobs'])
        self.assertFalse(list((self.service.root/'results').glob('*.wav')))

    def test_idle_child_exit_invalidates_runtime_and_explicit_prepare_recovers(self):
        starts = self.replace_worker()
        _, ready = self.prepare_ready()
        previous = ready['runtime']
        old = self.service.child
        old.terminate()
        old.wait(timeout=2)
        status = self.service.status()
        self.assertFalse(status['runtime_ready'])
        self.assertIsNone(status['runtime'])
        self.assertEqual(status['last_runtime'], previous)
        self.assertEqual(status['runtime_state'], 'failed')
        self.assertIn('exited with code', status['runtime_error'])
        time.sleep(.15)
        self.assertEqual(len(starts.read_text().splitlines()), 1)
        _, recovered = self.prepare_ready()
        self.assertNotEqual(self.service.child.pid, old.pid)
        self.assertEqual(recovered['runtime_state'], 'ready')
        self.assertEqual(len(starts.read_text().splitlines()), 2)

    def test_active_cancel_clears_runtime_before_prepare_recovers(self):
        self.replace_worker()
        job = self.submit('slow')
        self.until(lambda status: status['runtime_ready'] and self.service.active == job['id'])
        old = self.service.child
        self.service.cancel(job['id'])
        status = self.service.status()
        self.assertFalse(status['runtime_ready'])
        self.assertIsNone(status['runtime'])
        self.assertIsNotNone(status['last_runtime'])
        self.assertEqual(status['runtime_state'], 'stopped')
        self.assertIsNotNone(old.poll())
        self.prepare_ready()
        self.assertNotEqual(self.service.child.pid, old.pid)
        self.assertEqual(self.wait(job['id'])['status'], 'cancelled')
        self.assertEqual(self.wait(self.submit()['id'])['status'], 'completed')

    def test_cancel_before_write_closes_real_fileio_and_prepare_recovers(self):
        self.replace_worker()
        self.prepare_ready()
        old = self.service.child
        self.assertIsInstance(old.stdin, io.FileIO)
        entered, release = threading.Event(), threading.Event()
        write = self.service.write_child
        def paused(*args, **kwargs):
            entered.set()
            release.wait(timeout=2)
            return write(*args, **kwargs)
        with patch.object(self.service, 'write_child', side_effect=paused):
            job = self.submit()
            self.assertTrue(entered.wait(timeout=1))
            self.service.cancel(job['id'])
            self.assertTrue(old.stdin.closed)
            self.assertIsNotNone(old.poll())
            release.set()
            self.prepare_ready()
        status = self.service.status()
        self.assertTrue(status['worker_alive'])
        self.assertIsNone(status['worker_error'])
        self.assertNotEqual(self.service.child.pid, old.pid)
        self.assertEqual(self.wait(job['id'])['status'], 'cancelled')
        self.assertEqual(self.wait(self.submit()['id'])['status'], 'completed')

    def test_cancel_during_full_request_pipe_does_not_block_the_service(self):
        self.replace_worker("import json,time\nprint(json.dumps({'ready':True}),flush=True)\ntime.sleep(10)\n")
        self.prepare_ready()
        old = self.service.child
        descriptor = old.stdin.fileno()
        fcntl.fcntl(descriptor, fcntl.F_SETPIPE_SZ, 4096)
        self.service.voices['test']['transcript'] = 'x'*4000
        job = self.submit('word '*80, source_text='s'*2000)
        self.until(lambda status: not select.select([], [descriptor], [], 0)[1])
        self.service.cancel(job['id'])
        self.assertTrue(old.stdin.closed)
        Path(self.service.command[1]).write_text(RUNTIME)
        self.prepare_ready()
        status = self.service.status()
        self.assertTrue(status['worker_alive'])
        self.assertIsNone(status['worker_error'])
        self.assertEqual(self.wait(job['id'])['status'], 'cancelled')

    def test_stop_cancels_preparation_and_requires_new_prepare(self):
        starts = self.replace_worker(RUNTIME.replace("print('runtime diagnostic before ready')",
                                                     "time.sleep(.2)"))
        first = self.service.route('POST', '/api/speech/prepare', {})
        self.until(lambda status: status['runtime_state'] == 'preparing' and starts.exists()
                   and len(starts.read_text().splitlines()) == 1)
        old = self.service.child
        self.service.route('POST', '/api/speech/stop', {})
        stopped = self.service.status()
        self.assertFalse(stopped['runtime_ready'])
        self.assertEqual(stopped['runtime_state'], 'stopped')
        self.assertEqual(stopped['prepare']['status'], 'cancelled')
        self.assertIsNotNone(old.poll())
        response, ready = self.prepare_ready()
        self.assertNotEqual(response['prepare']['id'], first['prepare']['id'])
        self.assertEqual(ready['prepare']['status'], 'ready')
        self.assertEqual(len(starts.read_text().splitlines()), 2)

    def test_stop_before_prepare_enters_startup_does_not_launch_a_child(self):
        starts = self.replace_worker()
        entered, release = threading.Event(), threading.Event()
        ensure = self.service.ensure_child
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(timeout=2)
            return ensure(*args, **kwargs)
        with patch.object(self.service, 'ensure_child', side_effect=delayed):
            self.service.route('POST', '/api/speech/prepare', {})
            self.assertTrue(entered.wait(timeout=1))
            self.service.route('POST', '/api/speech/stop', {})
            release.set()
            self.until(lambda status: status['runtime_state'] == 'stopped')
        self.assertIsNone(self.service.child)
        self.assertFalse(starts.exists())
        self.prepare_ready()

    def test_child_crash_during_job_is_recoverable_and_keeps_exit_code(self):
        source = RUNTIME.replace(" j=json.loads(line)", " j=json.loads(line)\n if j['text']=='crash': sys.exit(7)")
        self.replace_worker(source)
        job = self.submit('crash')
        self.assertEqual(self.wait(job['id'])['status'], 'failed')
        status = self.service.status()
        self.assertFalse(status['runtime_ready'])
        self.assertIsNone(status['runtime'])
        self.assertIsNotNone(status['last_runtime'])
        self.assertEqual(status['runtime_exit_code'], 7)
        self.assertIsNone(status['worker_error'])
        self.assertTrue(status['worker_alive'])
        self.assertEqual(self.wait(self.submit()['id'])['status'], 'completed')

    def test_ready_message_requires_boolean_true(self):
        for ready in (False, 'yes', 1):
            with self.subTest(ready=ready):
                self.replace_worker('import json,time\nprint('+repr(json.dumps({'ready': ready}))+',flush=True)\ntime.sleep(10)\n')
                self.service.route('POST', '/api/speech/prepare', {})
                failed = self.until(lambda status: status['prepare']['status'] == 'failed')
                self.assertFalse(failed['runtime_ready'])
                self.assertIsNone(failed['last_runtime'])
                self.assertIn('did not become ready', failed['runtime_error'])

    def test_stopping_idle_runtime_clears_ready_and_keeps_history(self):
        self.replace_worker()
        _, ready = self.prepare_ready()
        child = self.service.child
        self.service.route('POST', '/api/speech/stop', {})
        status = self.service.status()
        self.assertFalse(status['runtime_ready'])
        self.assertIsNone(status['runtime'])
        self.assertEqual(status['last_runtime'], ready['runtime'])
        self.assertEqual(status['runtime_state'], 'stopped')
        self.assertIsNotNone(child.poll())
        self.prepare_ready()
        self.service.close()
        self.assertFalse(self.service.status()['runtime_ready'])
        self.assertEqual(self.service.status()['runtime_state'], 'closed')

    def test_prepare_failure_is_queryable_and_not_automatically_retried(self):
        source = "import json,time\nprint(json.dumps({'error':'Missing fake model'}),flush=True)\ntime.sleep(10)\n"
        self.replace_worker(source)
        first = self.service.route('POST', '/api/speech/prepare', {})
        failed = self.until(lambda status: status['prepare']['status'] == 'failed')
        self.assertFalse(failed['runtime_ready'])
        self.assertIsNone(failed['runtime'])
        self.assertIsNone(failed['last_runtime'])
        self.assertIn('Missing fake model', failed['runtime_error'])
        self.assertIn('Missing fake model', failed['warmup_error'])
        self.assertEqual(failed['prepare']['id'], first['prepare']['id'])
        self.assertTrue(self.service.worker.is_alive())
        child = self.service.child
        self.assertIsNotNone(child.poll())
        time.sleep(.15)
        self.assertIs(self.service.child, child)
        Path(self.service.command[1]).write_text(RUNTIME)
        with self.service.lock:
            response = self.service.route('POST', '/api/speech/prepare', {})
            self.assertEqual(response['runtime_state'], 'queued')
            self.assertIsNone(response['runtime_error'])
            self.assertIsNone(response['warmup_error'])
            accepted = self.service.status()
            self.assertIsNone(accepted['warmup_error'])
            self.assertEqual(accepted['last_prepare'], failed['prepare'])
            self.assertIn('Missing fake model', accepted['last_runtime_error']['message'])
        ready = self.until(lambda status: status['runtime_ready'])
        self.assertNotEqual(response['prepare']['id'], first['prepare']['id'])
        self.assertEqual(ready['runtime_state'], 'ready')
        self.assertIsNone(ready['warmup_error'])
        self.assertEqual(ready['last_prepare'], failed['prepare'])

    def test_prepare_timeout_is_finite_and_preload_uses_same_operation(self):
        self.replace_worker('import time\ntime.sleep(10)\n', timeout=.15, preload=True)
        failed = self.until(lambda status: status['prepare']['status'] == 'failed')
        self.assertFalse(failed['runtime_ready'])
        self.assertIn('timed out', failed['prepare']['error'])
        self.assertIsNotNone(self.service.child.poll())
        self.assertTrue(self.service.worker.is_alive())
        self.assertFalse(failed['jobs'])

    def test_internal_prepare_errors_stop_the_worker_and_remain_visible(self):
        for error in (AssertionError('Preparation assertion failed'), ValueError('Preparation value failed')):
            with self.subTest(error=type(error).__name__):
                self.replace_worker()
                with patch.object(self.service, 'read_child', side_effect=error):
                    self.service.route('POST', '/api/speech/prepare', {})
                    failed = self.until(lambda status: status['worker_error'] is not None)
                    self.service.worker.join(timeout=2)
                self.assertFalse(failed['runtime_ready'])
                self.assertEqual(failed['worker_error']['type'], type(error).__name__)
                self.assertEqual(failed['worker_error']['message'], str(error))
                self.assertFalse(self.service.worker.is_alive())
                self.assertIsNotNone(self.service.child.poll())
                for action in [lambda: self.service.route('POST', '/api/speech/prepare', {}), self.submit]:
                    with self.assertRaises(ApiError) as raised:
                        action()
                    self.assertEqual(raised.exception.code, 503)

    def test_internal_job_error_stops_consumption_instead_of_continuing(self):
        self.replace_worker()
        with patch.object(self.service, 'queue_audio', side_effect=AssertionError('Audio bookkeeping defect')):
            job = self.submit()
            failed = self.until(lambda status: status['worker_error'] is not None)
            self.service.worker.join(timeout=2)
        self.assertFalse(self.service.worker.is_alive())
        self.assertEqual(failed['worker_error']['type'], 'AssertionError')
        result = self.wait(job['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('Internal speech worker error', result['error'])
        self.assertFalse(failed['runtime_ready'])


    def test_marked_reject_keeps_child_and_next_job_uses_same_runtime(self):
        source = RUNTIME.replace(" j=json.loads(line)", " j=json.loads(line)\n"
            " if j['text']=='reject': print(json.dumps({'error':'known duration limit','rejected':True}),flush=True); continue")
        starts = self.replace_worker(source)
        _, ready = self.prepare_ready()
        pid = self.service.child.pid
        job = self.submit('reject')
        result = self.wait(job['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['rejected'])
        status = self.service.status()
        self.assertTrue(status['runtime_ready'])
        self.assertIsNone(status['worker_error'])
        self.assertEqual(status['runtime_pid'], pid)
        following = self.wait(self.submit()['id'])
        self.assertEqual(following['status'], 'completed')
        self.assertEqual(self.service.child.pid, pid)
        self.assertEqual(starts.read_text().splitlines(), [str(pid)])

    def test_unmarked_worker_error_still_invalidates_the_runtime(self):
        source = RUNTIME.replace(" j=json.loads(line)", " j=json.loads(line)\n"
            " if j['text']=='reject': print(json.dumps({'error':'unmarked failure'}),flush=True); continue")
        self.replace_worker(source)
        job = self.submit('reject')
        failed = self.wait(job['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('unmarked failure', failed['error'])
        status = self.service.status()
        self.assertFalse(status['runtime_ready'])
        self.assertIsNone(status['runtime'])
        self.assertEqual(status['runtime_state'], 'failed')
        self.assertTrue(status['worker_alive'])
        self.assertEqual(self.wait(self.submit()['id'])['status'], 'completed')

    def test_playback_disk_full_is_queryable_and_close_reclaims_processes(self):
        self.replace_worker()
        self.service.device = 'null'
        def write(path, value):
            if value.get('status') == 'playing':
                raise OSError(errno.ENOSPC, 'No space left on device')
            return atomic_json(path, value)
        with patch('speech_service.atomic_json', side_effect=write):
            job = self.submit(playback=True, live=True)
            result = self.wait(job['id'])
            self.assertEqual(result['status'], 'failed')
            status = self.service.status()
            self.assertFalse(status['runtime_ready'])
            self.assertIsNone(status['runtime'])
            self.assertEqual(status['worker_error']['component'], 'playback')
            self.assertIn('No space left', status['storage_warning'])
            self.assertTrue(status['storage_failed'])
            with self.assertRaises(ApiError):
                self.service.route('POST', '/api/speech/prepare', {})
            with self.assertRaises(ApiError):
                self.submit()
            self.service.close()
        self.assertFalse(self.service.worker.is_alive())
        self.assertFalse(self.service.play_worker.is_alive())
        self.assertFalse(self.service.status()['playback_worker_alive'])
        self.assertIsNotNone(self.service.child.poll())

    def test_playback_sqlite_failure_is_queryable_and_close_reclaims_processes(self):
        self.replace_worker(delivery_policy='coverage')
        self.service.device = 'null'
        original = DiskJobs.__setitem__
        def setitem(jobs, key, job):
            if job.get('status') == 'playing':
                raise sqlite3.OperationalError('disk I/O error')
            return original(jobs, key, job)
        with patch.object(DiskJobs, '__setitem__', new=setitem):
            job = self.submit(playback=True, live=True)
            result = self.wait(job['id'])
            self.assertEqual(result['status'], 'failed')
            status = self.service.status()
            self.assertFalse(status['runtime_ready'])
            self.assertEqual(status['worker_error']['component'], 'playback')
            self.assertIn('disk I/O error', status['storage_warning'])
            self.assertTrue(status['storage_failed'])
            self.service.close()
        self.assertFalse(self.service.worker.is_alive())
        self.assertFalse(self.service.play_worker.is_alive())
        self.assertIsNotNone(self.service.child.poll())

    def test_close_reclaims_threads_during_persistent_write_failure(self):
        self.replace_worker()
        job = self.submit('slow')
        self.until(lambda status: getattr(self.service, 'active', None) == job['id'] and self.service.child is not None)
        with patch.object(self.service, 'save_job', side_effect=OSError(errno.ENOSPC, 'No space left on device')):
            self.service.close()
        self.assertFalse(self.service.worker.is_alive())
        self.assertFalse(self.service.play_worker.is_alive())
        self.assertIsNotNone(self.service.child.poll())

    def test_unavailable_storage_rejects_new_work_before_fatal_handler_runs(self):
        self.replace_worker()
        with self.service.lock:
            self.service.note_storage_failure(OSError(errno.ENOSPC, 'No space left on device'))
        self.assertTrue(self.service.status()['storage_failed'])
        with self.assertRaises(ApiError):
            self.service.route('POST', '/api/speech/prepare', {})
        with self.assertRaises(ApiError):
            self.submit()

    def test_coverage_snapshot_failure_keeps_runtime_and_completes_the_task(self):
        self.replace_worker(delivery_policy='coverage')
        _, ready = self.prepare_ready()
        pid = self.service.child.pid
        original = atomic_json
        def write(path, value):
            if path.parent.name == 'results':
                raise OSError(errno.ENOSPC, 'No space left on device')
            return original(path, value)
        with patch('speech_service.atomic_json', side_effect=write):
            job = self.submit()
            result = self.wait(job['id'])
        self.assertEqual(result['status'], 'completed')
        status = self.service.status()
        self.assertTrue(status['runtime_ready'])
        self.assertIsNone(status['worker_error'])
        self.assertIn('No space left', status['storage_warning'])
        self.assertFalse(status['storage_failed'])
        self.assertEqual(status['runtime_pid'], pid)
        self.assertEqual(self.service.jobs[job['id']]['status'], 'completed')
        self.assertEqual(self.wait(self.submit()['id'])['status'], 'completed')

    def test_playback_launch_oserror_after_synthesis_fails_the_job_and_runtime(self):
        self.replace_worker(CHUNK_WORKER)
        self.service.device = 'null'
        original = subprocess.Popen
        calls = []
        def launch(command, **kwargs):
            if command[0] == 'aplay':
                calls.append(command)
                if len(calls) == 2:
                    raise OSError(errno.EIO, 'playback launch failed')
            return original(command, **kwargs)
        with patch('speech_service.subprocess.Popen', side_effect=launch):
            job = self.submit(' '.join(['chunk']*36), playback=True)
            result = self.wait(job['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['generation_done'])
        self.assertIn('playback launch failed', result['error'])
        self.assertIn('finished_at', result)
        status = self.service.status()
        self.assertFalse(status['runtime_ready'])
        self.assertEqual(status['worker_error']['component'], 'playback')
        self.until(lambda status: not status['playback_worker_alive'])

    def test_playback_programming_error_is_fatal_and_keeps_the_failed_job(self):
        self.replace_worker(CHUNK_WORKER)
        self.service.device = 'null'
        with patch.object(self.service, 'complete', side_effect=AssertionError('Playback bookkeeping defect')):
            job = self.submit(' '.join(['chunk']*36), playback=True)
            result = self.wait(job['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('Playback bookkeeping defect', result['error'])
        self.assertIn('finished_at', result)
        status = self.service.status()
        self.assertFalse(status['runtime_ready'])
        self.assertEqual(status['worker_error']['type'], 'AssertionError')
        self.assertEqual(status['worker_error']['component'], 'playback')

    def test_fatal_playback_error_ends_every_queued_playback_task(self):
        self.replace_worker(CHUNK_WORKER)
        release, entered = threading.Event(), threading.Event()
        class BlockedPlayer:
            stdin = None
            def __init__(self):
                self.returncode = None
            def wait(self, timeout=None):
                release.wait(timeout=timeout)
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode
            def poll(self):
                return self.returncode
            def terminate(self):
                self.returncode = -15
            def kill(self):
                self.returncode = -9
        launch_original = subprocess.Popen
        def launch(command, **kwargs):
            if command[0] == 'aplay':
                entered.set()
                return BlockedPlayer()
            return launch_original(command, **kwargs)
        with patch('speech_service.subprocess.Popen', side_effect=launch), patch.object(
                self.service, 'complete', side_effect=AssertionError('Playback bookkeeping defect')):
            first = self.submit(' '.join(['chunk']*36), playback=True)
            second = self.submit(' '.join(['chunk']*36), playback=True)
            self.assertTrue(entered.wait(timeout=3))
            deadline = time.monotonic()+4
            while time.monotonic() < deadline:
                with self.service.lock:
                    generated = [self.service.jobs[j['id']].get('generation_done') for j in (first, second)]
                    queued = len(self.service.play_pending)
                if all(generated) and queued >= 3:
                    break
                time.sleep(.01)
            self.assertEqual(generated, [True, True])
            self.assertGreaterEqual(queued, 3)
            release.set()
            self.assertEqual(self.wait(first['id'])['status'], 'failed')
            self.assertEqual(self.wait(second['id'])['status'], 'failed')
        status = self.service.status()
        self.assertEqual(status['worker_error']['component'], 'playback')
        self.assertEqual(status['worker_error']['type'], 'AssertionError')
        self.assertFalse(status['storage_failed'])
        self.assertFalse(status['runtime_ready'])
        self.assertFalse(self.service.play_pending)

    def test_submit_insert_failure_returns_507_and_stops_admission(self):
        self.replace_worker(delivery_policy='coverage')
        self.prepare_ready()
        self.assertFalse(self.service.status()['storage_failed'])
        original = DiskJobs.__setitem__
        def setitem(jobs, key, job):
            if job.get('text') == 'unstoreable':
                raise sqlite3.OperationalError('disk I/O error')
            return original(jobs, key, job)
        with patch.object(DiskJobs, '__setitem__', new=setitem):
            with self.assertRaises(ApiError) as raised:
                self.submit('unstoreable')
        self.assertEqual(raised.exception.code, 507)
        status = self.service.status()
        self.assertTrue(status['storage_failed'])
        self.assertIn('disk I/O error', status['storage_warning'])
        self.assertEqual(status['worker_error']['component'], 'submit')
        with self.assertRaises(ApiError) as rejected:
            self.submit()
        self.assertEqual(rejected.exception.code, 503)
        self.service.worker.join(timeout=2)
        self.assertFalse(self.service.worker.is_alive())

    def test_submit_pending_failure_returns_507_and_stops_admission(self):
        self.replace_worker(delivery_policy='coverage')
        self.prepare_ready()
        with patch.object(DiskPending, 'append', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(ApiError) as raised:
                self.submit()
        self.assertEqual(raised.exception.code, 507)
        status = self.service.status()
        self.assertTrue(status['storage_failed'])
        self.assertIn('database is locked', status['storage_warning'])
        self.assertEqual(status['worker_error']['component'], 'submit')
        with self.assertRaises(ApiError) as rejected:
            self.submit()
        self.assertEqual(rejected.exception.code, 503)

    def test_cancel_discard_failure_returns_507_and_stops_admission(self):
        self.replace_worker(delivery_policy='coverage')
        with self.service.lock:
            job = self.submit('slow')
            with patch.object(DiskPending, 'discard', side_effect=sqlite3.OperationalError('disk I/O error')):
                with self.assertRaises(ApiError) as raised:
                    self.service.cancel(job['id'])
        self.assertEqual(raised.exception.code, 507)
        status = self.service.status()
        self.assertTrue(status['storage_failed'])
        self.assertIn('disk I/O error', status['storage_warning'])
        self.assertEqual(status['worker_error']['component'], 'cancel')
        self.assertEqual(self.service.jobs.cache[job['id']]['status'], 'cancelled')
        with self.assertRaises(ApiError) as rejected:
            self.submit()
        self.assertEqual(rejected.exception.code, 503)
    def test_close_after_close_reclaims_threads_without_writing_storage(self):
        self.replace_worker(delivery_policy='coverage')
        self.service.close()
        writes = []
        def record(*args, **kwargs):
            writes.append(args)
        with patch.object(DiskJobs, '__setitem__', new=record), patch.object(DiskPending, 'discard', new=record):
            self.service.close()
        self.assertEqual(writes, [])
        self.assertFalse(self.service.worker.is_alive())
        self.assertFalse(self.service.play_worker.is_alive())

    def test_submit_disk_failure_fails_realtime_waiting_tasks_and_clears_queue(self):
        self.replace_worker()
        original = atomic_json
        def write(path, value):
            if value.get('text') == 'unstoreable':
                raise OSError(errno.ENOSPC, 'No space left on device')
            return original(path, value)
        with self.service.lock:
            waiting = [self.submit() for _ in range(2)]
            with patch('speech_service.atomic_json', side_effect=write):
                with self.assertRaises(ApiError) as raised:
                    self.submit('unstoreable')
            self.assertEqual(raised.exception.code, 507)
            self.assertFalse(self.service.pending)
            self.assertEqual([self.service.jobs[j['id']]['status'] for j in waiting],
                             ['failed', 'failed'])
        status = self.service.status()
        self.assertTrue(status['storage_failed'])
        self.assertIn('No space left', status['storage_warning'])
        self.assertEqual(status['worker_error']['component'], 'submit')
        self.assertEqual(status['unfinished_jobs'], 0)


if __name__ == '__main__':
    unittest.main()
