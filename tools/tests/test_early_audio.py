import errno
import io
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speech_service import SpeechService, atomic_json
from tempo_player import TempoPlayer
import test_speech_service as speech_tests

WORKER = '''import json,sys,time,wave
from pathlib import Path
print(json.dumps({'ready':True,'audio_stream_protocol':1}),flush=True)
for line in sys.stdin:
 j=json.loads(line);output=Path(j['output']);pieces=[];started=time.monotonic()
 for i in range(3):
  if i:
   time.sleep(.01 if j['text'].startswith('quick') else .12)
  if i==1 and j['text'].startswith('cancel'):
   time.sleep(10)
  if i==1 and j['text'].startswith('latefail'):
   print(json.dumps({'error':'late generation failure'}),flush=True);break
  if i==1 and j['text'].startswith('latereject'):
   print(json.dumps({'error':'late duration limit','rejected':True}),flush=True);break
  block=output.with_name(output.stem+f'-block-{i:05d}.wav')
  pcm=bytes([i+1,0])*1920;pieces.append(pcm)
  with wave.open(str(block),'wb') as w:
   w.setparams((1,2,24000,0,'NONE','not compressed'));w.writeframes(pcm)
  index=i+1 if j['text'].startswith('badorder') else i
  print(json.dumps({'type':'audio','block_index':index,'filename':block.name,
   'duration_seconds':.08,'sample_rate':24000,'synthesis_seconds':.12}),flush=True)
 else:
  if j['text'].startswith('latecomplete'):time.sleep(.2)
  with wave.open(str(output),'wb') as w:
   w.setparams((1,2,24000,0,'NONE','not compressed'));w.writeframes(b''.join(pieces))
  print(json.dumps({'type':'complete','stream_blocks':3,'duration_seconds':.24,
   'sample_rate':24000,'synthesis_seconds':time.monotonic()-started}),flush=True)
'''


class EarlyAudioTests(unittest.TestCase):
    submit = speech_tests.SpeechTests.submit
    wait = speech_tests.SpeechTests.wait

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        worker = self.root/'worker.py'
        worker.write_text(WORKER)
        self.players = []
        original = subprocess.Popen
        def launch(command, **kwargs):
            if command[0] == 'aplay':
                target = self.root/f'played-{len(self.players)}.pcm'
                self.players.append(target)
                command = [sys.executable, '-c',
                    'import sys,pathlib;pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())', str(target)]
            return original(command, **kwargs)
        self.player_patch = patch('speech_service.subprocess.Popen', side_effect=launch)
        self.player_patch.start()
        self.service = SpeechService(self.root, [sys.executable, str(worker)],
            playback_device='null', delivery_policy='coverage')
        self.service.voices['test'] = {'id':'test','revision':'fixed','transcript':'',
                                      'engine':'pocket','status':'needs_preview'}

    def tearDown(self):
        self.service.close()
        self.player_patch.stop()
        self.temp.cleanup()

    def realtime_service(self):
        command, voices = self.service.command, self.service.voices
        self.service.close()
        self.service = SpeechService(self.root, command, playback_device='null')
        self.service.voices = voices

    def first_play(self, job):
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            current = self.service.route('GET', f"/api/speech/jobs/{job['id']}", {})
            if 'playback_started_at' in current:
                return current
            if current['status'] == 'failed':
                self.fail(current.get('error'))
            time.sleep(.005)
        self.fail('No early playback')

    def test_first_block_plays_before_generation_ends_and_pcm_is_complete(self):
        job = self.submit('latecomplete', playback=True, live=True)
        started = self.first_play(job)
        self.assertFalse(started['generation_done'])
        result = self.wait(job['id'])
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertLess(result['playback_started_at'], result['generation_finished_at'])
        self.assertEqual(result['played_chunks'], 3)
        self.assertEqual(len(self.players), 1)
        with wave.open(str(self.root/'results'/f"{job['id']}.wav"), 'rb') as source:
            self.assertEqual(self.players[0].read_bytes(), source.readframes(source.getnframes()))

    def test_late_failure_marks_partial_playback_and_next_job_recovers(self):
        first = self.submit('latefail', playback=True, live=True)
        self.first_play(first)
        failed = self.wait(first['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('late generation failure', failed['error'])
        following = self.wait(self.submit('normal', playback=True, live=True)['id'])
        self.assertEqual(following['status'], 'completed', following.get('error'))

    def test_marked_late_reject_keeps_child_and_next_job_uses_same_pid(self):
        first = self.submit('latereject', playback=True, live=True)
        self.first_play(first)
        failed = self.wait(first['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('late duration limit', failed['error'])
        self.assertTrue(failed['rejected'])
        status = self.service.status()
        self.assertTrue(status['runtime_ready'])
        self.assertIsNone(status['worker_error'])
        self.assertFalse(status['storage_failed'])
        pid = self.service.child.pid
        following = self.wait(self.submit('normal', playback=True, live=True)['id'])
        self.assertEqual(following['status'], 'completed', following.get('error'))
        self.assertEqual(self.service.child.pid, pid)

    def test_marked_late_reject_during_a_paused_pipe_write_keeps_runtime_ready(self):
        """拒绝关闭播放管道后，写管道报错只结束该任务。"""
        entered, release = threading.Event(), threading.Event()
        real_select = select.select
        def paused_select(read, write, xlist, timeout=None):
            ready = real_select(read, write, xlist, timeout)
            if write and not read and not entered.is_set():
                entered.set()
                release.wait(timeout=3)
            return ready
        with patch('speech_service.select.select', side_effect=paused_select):
            first = self.submit('latereject', playback=True, live=True)
            self.assertTrue(entered.wait(timeout=2))
            failed = self.wait(first['id'])
            self.assertEqual(failed['status'], 'failed')
            self.assertIn('late duration limit', failed['error'])
            self.assertTrue(failed['rejected'])
            release.set()
            status = self.service.status()
            self.assertTrue(status['runtime_ready'])
            self.assertIsNone(status['worker_error'])
            self.assertFalse(status['storage_failed'])
            pid = self.service.child.pid
            self.assertEqual(status['runtime_pid'], pid)
            following = self.wait(self.submit('normal', playback=True, live=True)['id'])
        self.assertEqual(following['status'], 'completed', following.get('error'))
        self.assertEqual(self.service.child.pid, pid)

    def test_playback_pipe_write_failure_for_a_live_task_stays_fatal(self):
        """任务未结束时写播放管道失败，仍令播放组件致命失败。"""
        self.realtime_service()
        original = os.write
        def failing_write(descriptor, data):
            player = self.service.player
            current = None
            if player is not None and player.stdin is not None:
                try:
                    current = player.stdin.fileno()
                except ValueError:
                    current = None
            if current is not None and descriptor == current:
                raise OSError(errno.EIO, 'playback pipe write failed')
            return original(descriptor, data)
        with patch('speech_service.os.write', side_effect=failing_write):
            job = self.submit('quick', playback=True, live=True)
            result = self.wait(job['id'])
        self.assertEqual(result['status'], 'failed')
        status = self.service.status()
        self.assertEqual(status['worker_error']['component'], 'playback')
        self.assertEqual(status['worker_error']['type'], 'OSError')
        self.assertFalse(status['storage_failed'])
        self.assertFalse(status['runtime_ready'])

    def test_streamed_playback_disk_full_is_queryable_and_close_reclaims(self):
        self.realtime_service()
        def write(path, value):
            if value.get('status') == 'playing':
                raise OSError(errno.ENOSPC, 'No space left on device')
            return atomic_json(path, value)
        with patch('speech_service.atomic_json', side_effect=write):
            job = self.submit('latecomplete', playback=True, live=True)
            result = self.wait(job['id'])
            self.assertEqual(result['status'], 'failed')
            status = self.service.status()
            self.assertFalse(status['runtime_ready'])
            self.assertEqual(status['worker_error']['component'], 'playback')
            self.assertIn('No space left', status['storage_warning'])
            self.assertTrue(status['storage_failed'])
            self.service.close()
        self.assertFalse(self.service.worker.is_alive())
        self.assertFalse(self.service.play_worker.is_alive())
        self.assertIsNotNone(self.service.child.poll())

    def test_cancel_interrupts_the_pipe_and_worker(self):
        first = self.submit('cancel', playback=True, live=True)
        self.first_play(first)
        self.service.cancel(first['id'])
        self.assertEqual(self.wait(first['id'])['status'], 'cancelled')
        following = self.wait(self.submit('normal', playback=True, live=True)['id'])
        self.assertEqual(following['status'], 'completed', following.get('error'))

    def test_invalid_block_order_fails_before_playback(self):
        result = self.wait(self.submit('badorder', playback=True, live=True)['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('order or filename', result['error'])
        self.assertEqual(self.players, [])

    def test_jobs_keep_fifo_order_with_one_pipe_per_job(self):
        jobs = [self.submit(f'normal {i}', playback=True, live=True) for i in range(3)]
        results = [self.wait(j['id']) for j in jobs]
        self.assertTrue(all(j['status'] == 'completed' for j in results))
        self.assertEqual(len(self.players), 3)
        self.assertEqual([j['playback_started_at'] for j in results],
                         sorted(j['playback_started_at'] for j in results))

    def test_ready_blocks_keep_complete_persistent_progress(self):
        job = self.submit('quick', playback=True, live=True)
        result = self.wait(job['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['played_chunks'], 3)
        self.assertTrue(all(chunk['status'] == 'completed' for chunk in result['chunks']))
        stored = json.loads((self.root/'results'/f"{job['id']}.json").read_text())
        self.assertEqual(stored['played_chunks'], 3)

    def test_adaptive_tempo_saves_actual_output_and_preserves_synthesis(self):
        self.service.playback_tempo = 'adaptive'
        self.service.capture_playback = True
        with self.service.lock:
            job = self.submit('quick adaptive', playback=True, live=True)
            self.service.jobs[job['id']]['created_at'] -= 4
        result = self.wait(job['id'])
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertEqual(result['playback_tempo'], 1.05)
        self.assertGreater(result['playback_started_at'], result['playback_input_started_at'])
        self.assertEqual(result['playback_timing'], 'null_paced_filtered_pcm')
        self.assertEqual(result['played_chunks'], 3)
        with wave.open(str(self.root/'results'/result['playback_audio_filename']), 'rb') as capture:
            self.assertEqual(capture.getnframes(), result['playback_output_samples'])
            self.assertEqual(capture.readframes(capture.getnframes()), self.players[0].read_bytes())
        with wave.open(str(self.root/'results'/f"{job['id']}.wav"), 'rb') as source:
            self.assertEqual(source.getnframes(), 5760)

    def test_adaptive_tempo_keeps_empty_queue_and_preview_at_normal_speed(self):
        self.service.playback_tempo = 'adaptive'
        with self.service.lock:
            self.assertEqual(self.service.select_playback_tempo({'live': False}), 1)
            job = self.submit('normal', playback=True, live=True)
            self.assertEqual(self.service.select_playback_tempo(self.service.jobs[job['id']]), 1)
        result = self.wait(job['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['playback_tempo'], 1)

    def test_next_job_cannot_inherit_previous_player_first_audio_time(self):
        with self.service.lock:
            original = self.service.player
            self.service.player = Mock(spec=TempoPlayer, first_audio_at=time.time()-10)
            self.service.active_play = 'next'
            self.service.player_job_id = 'previous'
            job = {'id': 'next'}
            try:
                self.service.sync_playback_start(job)
                self.assertNotIn('playback_started_at', job)
                self.service.player_job_id = 'next'
                self.service.sync_playback_start(job)
                self.assertEqual(job['playback_started_at'], self.service.player.first_audio_at)
            finally:
                self.service.player = original
                self.service.active_play = self.service.player_job_id = None

    def test_flexible_ratio_preserves_whole_stream_and_filter_tail(self):
        self.service.playback_tempo = 'flexible'
        self.service.capture_playback = True
        with patch.object(self.service.tempo_controller, 'select', return_value={
                'tempo': 1.15, 'limited': False}):
            result = self.wait(self.submit('quick flexible', playback=True, live=True)['id'])
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertEqual(result['playback_tempo'], 1.15)
        with wave.open(str(self.root/'results'/f"{result['id']}.wav"), 'rb') as source:
            source_pcm = source.readframes(source.getnframes())
        expected = subprocess.run(['ffmpeg', '-v', 'error', '-f', 's16le', '-ar', '24000',
            '-ac', '1', '-i', 'pipe:0', '-af', 'atempo=1.150', '-f', 's16le', 'pipe:1'],
            input=source_pcm, stdout=subprocess.PIPE, check=True).stdout
        self.assertEqual(self.players[0].read_bytes(), expected)
        self.assertEqual(result['playback_output_samples'], len(expected)//2)

    def test_flexible_complete_wav_path_also_waits_for_filter_tail(self):
        self.service.close()
        worker = self.root/'whole-worker.py'
        worker.write_text('''import json,sys,wave
print(json.dumps({'ready':True}),flush=True)
for line in sys.stdin:
 j=json.loads(line)
 with wave.open(j['output'],'wb') as w:
  w.setparams((1,2,24000,0,'NONE','not compressed'));w.writeframes(b'\\x10\\x00'*5760)
 print(json.dumps({'duration_seconds':.24,'sample_rate':24000}),flush=True)
''')
        self.service = SpeechService(self.root/'whole', [sys.executable, str(worker)],
            playback_device='null', delivery_policy='coverage', playback_tempo='flexible', capture_playback=True)
        self.service.voices['test'] = {'id':'test','revision':'fixed','transcript':'','engine':'pocket','status':'needs_preview'}
        with patch.object(self.service.tempo_controller, 'select', return_value={'tempo':1.037}):
            result = self.wait(self.submit('whole', playback=True, live=True)['id'])
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertTrue(result['continuous_playback'])
        self.assertGreater(result['playback_output_samples'], 0)
        self.assertEqual(result['played_chunks'], result['chunk_count'])


if __name__ == '__main__':
    unittest.main()
