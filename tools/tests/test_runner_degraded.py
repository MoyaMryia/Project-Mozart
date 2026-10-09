import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_translated_speech as runner


class RunnerDegradedTests(unittest.TestCase):
    def run_stack(self, missing_optional=False, gpu=False, threads=2, early=False):
        capture_started, speech_ready = threading.Event(), threading.Event()
        launches, processes, commands = [], [], {}
        def popen(command, **kwargs):
            if any('mozart_stated' in word for word in command):
                name = 'backend'
            elif any('speech_service.py' in word for word in command):
                name = 'speech'
            elif any('llama-server' in word for word in command):
                name = 'translation'
            elif any('subtitle_bridge.py' in word for word in command):
                name = 'captions'
                self.assertFalse(speech_ready.is_set())
            else:
                name = 'capture'
                capture_started.set()
            launches.append(name)
            commands[name] = (command, kwargs)
            if missing_optional and name in ('speech', 'translation'):
                raise FileNotFoundError(name)
            process = Mock(pid=1000+len(processes))
            process.poll.return_value = 0 if name == 'capture' else None
            def finish(*args, **kwargs):
                process.poll.return_value = 0
                return 0
            process.wait.side_effect = finish
            process.terminate.side_effect = finish
            processes.append(process)
            return process
        def urlopen(request, **kwargs):
            url = request if isinstance(request, str) else request.full_url
            if '/api/speech/status' in url and not isinstance(request, str) and threading.current_thread() is not threading.main_thread():
                self.assertTrue(capture_started.wait(3), 'Capture must start before speech warmup completes')
                speech_ready.set()
            response = Mock(status=200)
            response.read.return_value = json.dumps({'runtime': {'engine': 'fake'}, 'jobs': []}).encode()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            return response
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        sock.bind.side_effect = OSError('ASR is listening')
        options = ['--tts-provider', 'cuda', '--tts-precision', 'float32',
                   '--tts-pythonpath', '/isolated/gpu', '--tts-provider-config', '/isolated/provider.conf'] if gpu else []
        if threads != 2:
            options += ['--tts-threads', str(threads)]
        if early:
            options += ['--early-audio']
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(sys, 'argv', ['runner', '--backend-config', 'fake.toml', '--run-dir', directory]+options), \
                patch.object(runner, 'assert_ports_available'), \
                patch.object(runner.subprocess, 'Popen', side_effect=popen), \
                patch.object(runner.urllib.request, 'urlopen', side_effect=urlopen), \
                patch.object(runner.socket, 'socket', return_value=sock), \
                patch.object(runner.signal, 'signal'), \
                patch.object(runner.os, 'killpg'), \
                patch.object(runner, 'print'):
            try:
                runner.main()
                status = json.loads((Path(directory)/'speech-results.json').read_text())
            finally:
                capture_started.set()
        self.assertIn('captions', launches)
        self.assertIn('capture', launches)
        for process in processes:
            self.assertEqual(process.poll(), 0)
        speech_command = commands['speech'][0]
        self.assertEqual('--early-audio' in speech_command, early)
        self.assertEqual(speech_command[speech_command.index('--threads')+1], str(threads))
        translator_command = commands['translation'][0]
        self.assertEqual(translator_command[translator_command.index('-t')+1], '2')
        self.assertEqual(speech_command[speech_command.index('--delivery-policy')+1], 'coverage')
        caption_command = commands['captions'][0]
        self.assertEqual(caption_command[caption_command.index('--delivery-policy')+1], 'coverage')
        if gpu:
            command, settings = commands['speech']
            self.assertEqual(command[command.index('--provider')+1], 'cuda')
            self.assertEqual(command[command.index('--precision')+1], 'float32')
            self.assertEqual(command[command.index('--provider-config')+1], '/isolated/provider.conf')
            self.assertTrue(settings['env']['PYTHONPATH'].startswith('/isolated/gpu:'))
            for name in ('backend', 'translation', 'captions', 'capture'):
                environment = commands[name][1].get('env') or {}
                self.assertNotIn('/isolated/gpu', environment.get('PYTHONPATH', ''))
        return status

    def test_capture_starts_while_speech_is_still_warming(self):
        self.run_stack()

    def test_missing_translation_and_speech_keep_source_stack_running(self):
        status = self.run_stack(missing_optional=True)
        self.assertFalse(status['available'])

    def test_gpu_runtime_is_isolated_from_captions_and_translation(self):
        self.run_stack(gpu=True)

    def test_speech_threads_do_not_change_translation_threads(self):
        self.run_stack(threads=3)

    def test_early_audio_reaches_only_the_speech_service(self):
        self.run_stack(early=True)

    def test_invalid_speech_threads_fail_before_launch(self):
        with patch.object(sys, 'argv', ['runner', '--backend-config', 'fake.toml', '--tts-threads', '0']), \
                patch.object(runner.subprocess, 'Popen') as launch, patch.object(sys, 'stderr'):
            with self.assertRaises(SystemExit) as error:
                runner.main()
        self.assertEqual(error.exception.code, 2)
        launch.assert_not_called()

    def test_disk_backlog_is_not_hidden_by_recent_job_list(self):
        self.assertEqual(runner.unfinished_speech({'jobs': [], 'unfinished_jobs': 200}), 200)
        self.assertEqual(runner.unfinished_speech({'jobs': [{'status': 'completed'}]}), 0)


if __name__ == '__main__':
    unittest.main()
