import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speech_service import SpeechService
import test_speech_service as speech_tests

WORKER = '''import json,sys,time,wave
from pathlib import Path
print(json.dumps({'ready':True,'audio_stream_protocol':1}),flush=True)
for line in sys.stdin:
 j=json.loads(line);output=Path(j['output']);pieces=[];started=time.monotonic()
 for i in range(3):
  if i:
   time.sleep(.12)
  if i==1 and j['text'].startswith('cancel'):
   time.sleep(10)
  if i==1 and j['text'].startswith('latefail'):
   print(json.dumps({'error':'late generation failure'}),flush=True);break
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


if __name__ == '__main__':
    unittest.main()
