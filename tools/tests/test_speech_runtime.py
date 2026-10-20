"""用真实子进程检查运行时就绪、准备操作和故障恢复。"""
import fcntl
import io
import json
import select
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speech_service import ApiError, SpeechService
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_speech_service as support


RUNTIME = support.FAKE.replace('import json, sys, time, wave', '''import json, sys, time, wave, os
from pathlib import Path
with Path(__file__).with_suffix('.starts').open('a') as starts:
 starts.write(str(os.getpid())+'\\n')''').replace("{'ready': True}", "{'ready': True, 'worker_pid': os.getpid()}")


class SpeechRuntimeTests(unittest.TestCase):
    setUp = support.SpeechTests.setUp
    tearDown = support.SpeechTests.tearDown
    submit = support.SpeechTests.submit
    wait = support.SpeechTests.wait

    def replace_worker(self, source=RUNTIME, timeout=1., preload=False):
        voices = self.service.voices
        self.service.close()
        worker = Path(self.temp.name)/'runtime.py'
        worker.write_text(source)
        self.service = SpeechService(self.temp.name, [sys.executable, str(worker)],
                                     timeout=timeout, preload=preload)
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


if __name__ == '__main__':
    unittest.main()
