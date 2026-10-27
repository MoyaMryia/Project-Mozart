import json
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_translated_speech as runner


class RunnerReadinessTests(unittest.TestCase):
    def run_scenario(self, *, options=(), scenario='', expected_error=None, microphone=False):
        self.launches, self.snapshots, self.processes = [], [], {}
        self.request_urls = []
        health_calls = speech_calls = session_calls = 0
        self.voice_registrations = self.session_posts = 0
        self.translation_regressed = False
        auto_voice = None
        handlers = {}
        clock = [0.]
        regression_active = False
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            reference = run_dir/'reference.wav'
            reference.write_bytes(b'WAV validated by the speech service')
            argv = ['runner', '--backend-config', 'fake.yaml', '--run-dir', directory]
            if not microphone:
                argv += ['--input', '/fixture/input.mp4']
            argv += [str(reference) if value == 'REFERENCE' else value for value in options]

            self.launch_environments = []

            def snapshot():
                path = run_dir/'stack-status.json'
                if path.exists():
                    self.snapshots.append(json.loads(path.read_text()))

            def popen(command, **kwargs):
                nonlocal health_calls, speech_calls, session_calls
                snapshot()
                self.launch_environments.append((list(command), kwargs.get('env')))
                if any('mozart_stated' in word for word in command):
                    name = 'backend'
                elif any('speech_service.py' in word for word in command):
                    name = 'speech'
                elif any('llama-server' in word for word in command):
                    name = 'translation'
                elif any('subtitle_bridge.py' in word for word in command):
                    name = 'captions'
                    if '--mode' in argv and argv[argv.index('--mode')+1] == 'captions':
                        self.assertNotIn('--speak', command)
                    else:
                        self.assertIn('--speak', command)
                else:
                    name = 'microphone'
                    if '--allow-degraded' not in argv:
                        self.assertGreaterEqual(health_calls, 2)
                        if '--mode' not in argv:
                            self.assertGreaterEqual(speech_calls, 2)
                            self.assertGreaterEqual(session_calls, 1)
                    if scenario == 'child_exit':
                        self.processes['translation'].returncode = 7
                self.launches.append(name)
                if scenario == 'missing_translation' and name == 'translation':
                    raise FileNotFoundError('translator missing')
                process = Mock(pid=2000+len(self.processes), returncode=None)
                if name == 'microphone':
                    process.returncode = 3 if scenario == 'capture_failure' else 0
                    if scenario in ('child_exit', 'ready_exit', 'speech_exit', 'runtime_regression',
                                    'warmup_regression', 'health_regression', 'session_regression', 'mixed_recovery'):
                        process.returncode = None
                process.poll.side_effect = lambda: process.returncode
                def wait(**_):
                    if process.returncode is None:
                        process.returncode = 0
                    return process.returncode
                process.wait.side_effect = wait
                process.terminate.side_effect = wait
                self.processes[name] = process
                if name == 'captions' and '--external-capture' in argv and scenario != 'external_no_audio':
                    self.write_audio_status = lambda last: (run_dir/'audio-status.json').write_text(json.dumps({
                        'packets': 500, 'rejected': 0, 'last_audio_at': last,
                        'instance_id': self.snapshots[-1]['instance_id']}))
                    self.write_audio_status(time.time())
                return process

            def urlopen(request, **kwargs):
                nonlocal health_calls, speech_calls, session_calls, auto_voice
                url = request if isinstance(request, str) else request.full_url
                self.request_urls.append(url)
                if '/health' in url:
                    health_calls += 1
                    if health_calls == 1 or scenario == 'translation_timeout' or (scenario == 'health_regression' and regression_active):
                        raise urllib.error.URLError('model loading')
                    if scenario == 'mixed_recovery' and regression_active and not self.translation_regressed:
                        self.translation_regressed = True
                        raise urllib.error.URLError('temporary translator failure')
                    result = {'status': 'ok'}
                elif url.endswith('/api/speech/status'):
                    speech_calls += 1
                    if scenario == 'internal_error':
                        raise AssertionError('internal probe failure')
                    result = {'runtime': {'engine': 'fake'} if speech_calls >= 2 else None,
                              'runtime_ready': speech_calls >= 2, 'jobs': []}
                    if scenario == 'warmup_failure' or (scenario == 'warmup_regression' and regression_active):
                        result['warmup_error'] = 'model warmup failed'
                    if scenario in ('runtime_regression', 'keep_open_runtime_regression', 'mixed_recovery') and regression_active:
                        result['runtime'] = None
                        result['runtime_ready'] = False
                    if scenario == 'stale_runtime':
                        result.update(runtime={'engine': 'old process'}, runtime_ready=False,
                                      runtime_error='Speech worker exited with code 7')
                elif url.endswith('/api/voices'):
                    self.voice_registrations += 1
                    self.assertIn('audio_base64', json.loads(request.data))
                    result = {'id': 'reference-id'}
                elif url.endswith('/api/speech/session'):
                    session_calls += 1
                    if request.data:
                        self.session_posts += 1
                        if scenario == 'session_retry' and self.session_posts == 1:
                            raise urllib.error.URLError('temporary activation failure')
                        auto_voice = json.loads(request.data)
                        result = auto_voice
                    else:
                        result = auto_voice or {'enabled': session_calls >= 3, 'voice_id': 'ui-selected',
                                                'language': 'en', 'playback': True}
                        if scenario == 'stop_waiting':
                            result['enabled'] = False
                        if scenario == 'session_regression' and regression_active:
                            result['enabled'] = False
                else:
                    result = {}
                response = Mock(status=200)
                response.read.return_value = json.dumps(result).encode()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                return response

            def sleep(seconds):
                nonlocal regression_active
                clock[0] += seconds
                snapshot()
                if scenario == 'keep_open_runtime_regression' and self.snapshots[-1]['phase'] == 'preview':
                    regression_active = True
                if scenario in ('ready_exit', 'speech_exit', 'runtime_regression', 'warmup_regression',
                                'health_regression', 'session_regression', 'mixed_recovery'):
                    current = self.snapshots[-1]
                    if not regression_active and all(current['readiness'].values()):
                        regression_active = True
                        if scenario in ('ready_exit', 'speech_exit'):
                            self.processes['translation' if scenario == 'ready_exit' else 'speech'].returncode = 9
                    elif regression_active and (current['degraded'] or clock[0] > 5):
                        if scenario != 'mixed_recovery' or (current['readiness']['translation']
                                and not current['readiness']['speech_runtime']) or clock[0] > 5:
                            handlers[signal.SIGTERM](signal.SIGTERM, None)
                if scenario == 'stop_external_capture' and self.snapshots[-1]['phase'] == 'capturing':
                    handlers[signal.SIGTERM](signal.SIGTERM, None)
                if scenario == 'external_audio_stops' and self.snapshots[-1]['phase'] == 'capturing':
                    self.write_audio_status(time.time() - runner.STARTUP_TIMEOUT_SECONDS - 1)
                if scenario == 'external_child_exit' and self.snapshots[-1]['phase'] == 'capturing':
                    self.processes['translation'].returncode = 7
                if ((scenario == 'stop_waiting' and self.snapshots[-1]['phase'] == 'waiting_for_voice')
                        or (scenario == 'stop_loading' and self.snapshots[-1]['phase'] == 'preparing_services')):
                    handlers[signal.SIGTERM](signal.SIGTERM, None)

            def decode(*args, **kwargs):
                if scenario == 'decode_failure':
                    raise runner.subprocess.CalledProcessError(1, args[0])

            sock = Mock()
            sock.__enter__ = Mock(return_value=sock)
            sock.__exit__ = Mock(return_value=False)
            sock.bind.side_effect = OSError('ASR has bound its socket')
            with patch.object(sys, 'argv', argv), \
                    patch.object(runner, 'assert_ports_available') as ports, \
                    patch.object(runner.subprocess, 'Popen', side_effect=popen), \
                    patch.object(runner.subprocess, 'run', side_effect=decode), \
                    patch.object(runner.urllib.request, 'urlopen', side_effect=urlopen), \
                    patch.object(runner.socket, 'socket', return_value=sock), \
                    patch.object(runner.signal, 'signal', side_effect=lambda sig, handler: handlers.update({sig: handler})), \
                    patch.object(runner.os, 'killpg'), \
                    patch.object(runner.time, 'sleep', side_effect=sleep), \
                    patch.object(runner.time, 'monotonic', side_effect=lambda: clock[0]), \
                    patch.object(runner, 'print'):
                if expected_error:
                    with self.assertRaises(expected_error):
                        runner.main()
                else:
                    runner.main()
            self.checked_ports = ports.call_args.args[0]
            self.status = json.loads((run_dir/'stack-status.json').read_text())
            self.speech_calls = speech_calls
            self.snapshots.append(self.status)
            for process in self.processes.values():
                self.assertIsNotNone(process.poll())
            return self.status

    def test_full_mode_waits_for_models_and_ui_voice_selection(self):
        status = self.run_scenario()
        self.assertEqual(status['state'], 'completed')
        self.assertTrue(all(status['readiness'].values()))
        self.assertIn('waiting_for_voice', [item['phase'] for item in self.snapshots])
        self.assertIsNotNone(status['capture_started_at'])
        self.assertFalse(status['degraded'])

    def test_cli_reference_is_registered_and_selected_before_capture(self):
        status = self.run_scenario(options=['--reference', 'REFERENCE'])
        self.assertEqual(status['state'], 'completed')
        self.assertTrue(status['readiness']['voice_session'])

    def test_waiting_for_ui_voice_is_stoppable(self):
        status = self.run_scenario(scenario='stop_waiting')
        self.assertEqual(status['state'], 'stopped')
        self.assertNotIn('microphone', self.launches)
        self.assertIsNone(status['capture_started_at'])

    def test_stopping_model_warmup_prevents_capture(self):
        status = self.run_scenario(scenario='stop_loading')
        self.assertEqual(status['state'], 'stopped')
        self.assertNotIn('microphone', self.launches)

    def test_file_decode_failure_is_terminal_before_capture(self):
        status = self.run_scenario(scenario='decode_failure', expected_error=runner.subprocess.CalledProcessError)
        self.assertEqual(status['state'], 'failed')
        self.assertEqual(status['error']['type'], 'CalledProcessError')
        self.assertNotIn('microphone', self.launches)

    def test_glossary_and_corrections_reach_every_child(self):
        with tempfile.TemporaryDirectory() as labels:
            glossary = Path(labels)/'glossary.json'
            corrections = Path(labels)/'corrections.json'
            glossary.write_text('{"赌博": "gambling"}')
            corrections.write_text('{"痛赌": "赌博"}')
            self.run_scenario(options=['--glossary', str(glossary), '--corrections', str(corrections)])
            for name in ('mozart_stated', 'subtitle_bridge.py', 'llama-server'):
                found = [environment for command, environment in self.launch_environments
                         if any(name in str(word) for word in command)]
                self.assertEqual(len(found), 1, name)
                self.assertEqual(found[0]['MOZART_GLOSSARY'], str(glossary.resolve()), name)
            found = [environment for command, environment in self.launch_environments
                     if any('subtitle_bridge.py' in str(word) for word in command)]
            self.assertEqual(found[0]['MOZART_CORRECTIONS'], str(corrections.resolve()))

    def test_custom_api_port_reaches_probes_and_requests(self):
        self.run_scenario(options=['--api-port', '18181', '--mode', 'captions'])
        api_requests = [url for url in self.request_urls if '/api/' in url]
        self.assertTrue(api_requests)
        self.assertTrue(all(url.startswith('http://127.0.0.1:18181/') for url in api_requests))
        self.assertEqual(self.checked_ports[0], (18181, runner.socket.SOCK_STREAM))
        self.assertEqual(self.launches.count('backend'), 1)

    def test_api_port_rejects_invalid_values_before_launch(self):
        for port in ('0', '65536'):
            with self.subTest(port=port), \
                    patch.object(sys, 'argv', ['runner', '--backend-config', 'fake', '--api-port', port]), \
                    patch.object(runner.subprocess, 'Popen') as launch, patch.object(sys, 'stderr'):
                with self.assertRaises(SystemExit) as error:
                    runner.main()
                self.assertEqual(error.exception.code, 2)
                launch.assert_not_called()

    def test_external_capture_stops_without_launching_another_microphone(self):
        status = self.run_scenario(microphone=True, options=['--external-capture', '--mode', 'captions'],
                                   scenario='stop_external_capture')
        self.assertEqual(status['state'], 'stopped')
        self.assertIsNotNone(status['capture_started_at'])
        self.assertNotIn('microphone', self.launches)

    def test_external_capture_monitors_owned_services(self):
        status = self.run_scenario(microphone=True, options=['--external-capture', '--mode', 'captions'],
                                   scenario='external_child_exit', expected_error=RuntimeError)
        self.assertEqual(status['state'], 'failed')
        self.assertEqual(status['children']['translation']['exit_code'], 7)
        self.assertNotIn('microphone', self.launches)

    def test_external_capture_waits_for_audio_before_declaring_capture(self):
        status = self.run_scenario(microphone=True, options=['--external-capture', '--mode', 'captions'],
                                   scenario='external_no_audio', expected_error=runner.ServiceUnavailable)
        self.assertEqual(status['state'], 'failed')
        self.assertIsNone(status['capture_started_at'])
        self.assertNotIn('running', [item['state'] for item in self.snapshots])

    def test_external_capture_stops_when_audio_becomes_stale(self):
        status = self.run_scenario(microphone=True, options=['--external-capture', '--mode', 'captions'],
                                   scenario='external_audio_stops', expected_error=runner.ServiceUnavailable)
        self.assertEqual(status['state'], 'failed')
        self.assertIn('External audio unavailable', status['error']['message'])
        self.assertIsNotNone(status['capture_started_at'])

    def test_external_capture_rejects_file_replay(self):
        with patch.object(sys, 'argv', ['runner', '--backend-config', 'fake',
                                        '--external-capture', '--input', '/fixture/input.mp4']), \
                patch.object(runner.subprocess, 'Popen') as launch, patch.object(sys, 'stderr'):
            with self.assertRaises(SystemExit) as error:
                runner.main()
            self.assertEqual(error.exception.code, 2)
            launch.assert_not_called()

    def test_capture_preprocessor_does_not_take_the_realtime_control_port(self):
        # 实时变声链路占用 19040。字幕栈的采集预处理器必须关闭控制通道。
        self.run_scenario(options=['--mic-device', 'dsnoop:CARD=MIC,DEV=0'])
        found = [command for command, _ in self.launch_environments
                 if any('mozart-pre' in str(word) for word in command)]
        self.assertEqual(len(found), 1)
        self.assertIn('--ctl-port', found[0])
        self.assertEqual(found[0][found[0].index('--ctl-port')+1], '0')

    def test_captions_mode_has_no_speech_process(self):
        status = self.run_scenario(options=['--mode', 'captions'])
        self.assertEqual(status['state'], 'completed')
        self.assertNotIn('speech', self.launches)
        self.assertTrue(status['readiness']['translation'])

    def test_default_mode_missing_translator_fails_before_capture(self):
        status = self.run_scenario(scenario='missing_translation', expected_error=runner.ServiceUnavailable)
        self.assertEqual(status['state'], 'failed')
        self.assertNotIn('microphone', self.launches)
        self.assertIsNone(status['capture_started_at'])

    def test_translation_health_timeout_prevents_capture(self):
        status = self.run_scenario(scenario='translation_timeout', expected_error=runner.ServiceUnavailable)
        self.assertEqual(status['state'], 'failed')
        self.assertNotIn('microphone', self.launches)

    def test_warmup_failure_prevents_capture(self):
        status = self.run_scenario(scenario='warmup_failure', expected_error=runner.ServiceUnavailable)
        self.assertEqual(status['state'], 'failed')
        self.assertIn('model warmup failed', status['error']['message'])
        self.assertNotIn('microphone', self.launches)
        self.assertEqual(self.speech_calls, 1)

    def test_cached_runtime_metadata_does_not_admit_capture(self):
        status = self.run_scenario(scenario='stale_runtime', expected_error=runner.ServiceUnavailable)
        self.assertEqual(status['state'], 'failed')
        self.assertFalse(status['readiness']['speech_runtime'])
        self.assertNotIn('microphone', self.launches)
        self.assertIn('code 7', status['error']['message'])

    def test_internal_program_error_is_not_downgraded(self):
        status = self.run_scenario(scenario='internal_error', expected_error=AssertionError)
        self.assertEqual(status['state'], 'failed')
        self.assertEqual(status['error']['type'], 'AssertionError')
        self.assertNotIn('microphone', self.launches)

    def test_internal_error_is_not_hidden_in_explicit_degraded_mode(self):
        status = self.run_scenario(options=['--allow-degraded'], scenario='internal_error',
                                   expected_error=AssertionError)
        self.assertEqual(status['state'], 'failed')
        self.assertEqual(status['error']['type'], 'AssertionError')

    def test_capture_start_failure_never_publishes_running(self):
        status = self.run_scenario(scenario='capture_failure', expected_error=RuntimeError)
        self.assertEqual(status['state'], 'failed')
        self.assertIsNone(status['capture_started_at'])
        self.assertNotIn('running', [item['state'] for item in self.snapshots])
        self.assertEqual(status['children']['microphone']['exit_code'], 3)

    def test_service_exit_during_capture_fails_and_cleans_up(self):
        status = self.run_scenario(scenario='child_exit', expected_error=RuntimeError)
        self.assertEqual(status['state'], 'failed')
        self.assertEqual(status['children']['translation']['exit_code'], 7)
        self.assertIsNotNone(status['capture_started_at'])

    def test_microphone_exit_without_stop_request_is_failure(self):
        status = self.run_scenario(microphone=True, expected_error=RuntimeError)
        self.assertEqual(status['state'], 'failed')
        self.assertIsNone(status['capture_started_at'])

    def test_ready_translation_exit_retains_degraded_reason(self):
        status = self.run_scenario(options=['--allow-degraded'], scenario='ready_exit')
        self.assertEqual(status['state'], 'stopped')
        self.assertFalse(status['readiness']['translation'])
        self.assertIn('exited (9)', status['degraded']['translation'])
        self.assertEqual(status['children']['translation']['exit_code'], 9)

    def test_ready_speech_exit_retains_reason_and_clears_readiness(self):
        status = self.run_scenario(options=['--allow-degraded'], scenario='speech_exit')
        self.assertFalse(status['readiness']['speech_runtime'])
        self.assertFalse(status['readiness']['voice_session'])
        self.assertIn('exited (9)', status['degraded']['speech'])

    def test_runtime_regression_clears_runtime_and_voice_readiness(self):
        status = self.run_scenario(options=['--allow-degraded'], scenario='runtime_regression')
        self.assertFalse(status['readiness']['speech_runtime'])
        self.assertFalse(status['readiness']['voice_session'])
        self.assertIn('runtime became unavailable', status['degraded']['speech'])

    def test_warmup_error_after_ready_clears_runtime_and_voice_readiness(self):
        status = self.run_scenario(options=['--allow-degraded'], scenario='warmup_regression')
        self.assertFalse(status['readiness']['speech_runtime'])
        self.assertFalse(status['readiness']['voice_session'])
        self.assertIn('model warmup failed', status['degraded']['speech'])

    def test_full_runtime_regression_stops_capture(self):
        status = self.run_scenario(scenario='runtime_regression', expected_error=runner.ServiceUnavailable)
        self.assertEqual(status['state'], 'failed')
        self.assertFalse(status['readiness']['speech_runtime'])
        self.assertFalse(status['readiness']['voice_session'])

    def test_full_warmup_regression_stops_capture(self):
        status = self.run_scenario(scenario='warmup_regression', expected_error=runner.ServiceUnavailable)
        self.assertEqual(status['state'], 'failed')
        self.assertIn('model warmup failed', status['error']['message'])

    def test_full_translation_health_regression_stops_capture(self):
        status = self.run_scenario(scenario='health_regression', expected_error=runner.ServiceUnavailable)
        self.assertEqual(status['state'], 'failed')
        self.assertFalse(status['readiness']['translation'])

    def test_full_session_disabled_stops_capture(self):
        status = self.run_scenario(scenario='session_regression', expected_error=runner.ServiceUnavailable)
        self.assertEqual(status['state'], 'failed')
        self.assertFalse(status['readiness']['voice_session'])

    def test_full_runtime_regression_during_preview_fails(self):
        status = self.run_scenario(options=['--keep-open'], scenario='keep_open_runtime_regression',
                                   expected_error=runner.ServiceUnavailable)
        self.assertEqual(status['state'], 'failed')
        self.assertIn('preview', [item['phase'] for item in self.snapshots])
        self.assertFalse(status['readiness']['speech_runtime'])

    def test_reference_activation_retry_reuses_registered_voice(self):
        status = self.run_scenario(options=['--reference', 'REFERENCE'], scenario='session_retry')
        self.assertEqual(status['state'], 'completed')
        self.assertEqual(self.voice_registrations, 1)
        self.assertEqual(self.session_posts, 2)
        self.assertEqual(status['reference_voice_id'], 'reference-id')

    def test_translation_recovery_clears_its_reason_during_speech_failure(self):
        status = self.run_scenario(options=['--allow-degraded'], scenario='mixed_recovery')
        self.assertTrue(self.translation_regressed)
        self.assertTrue(status['readiness']['translation'])
        self.assertFalse(status['readiness']['speech_runtime'])
        self.assertNotIn('translation', status['degraded'])
        self.assertIn('speech', status['degraded'])

    def test_nonfinite_cli_values_fail_before_launch(self):
        for option, value in [('--start', 'nan'), ('--seconds', 'inf'), ('--seconds', 'nan'),
                              ('--boundary-threshold', 'nan')]:
            with self.subTest(option=option, value=value), \
                    patch.object(sys, 'argv', ['runner', '--backend-config', 'fake', option, value]), \
                    patch.object(runner.subprocess, 'Popen') as launch, patch.object(sys, 'stderr'):
                with self.assertRaises(SystemExit) as error:
                    runner.main()
                self.assertEqual(error.exception.code, 2)
                launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
