"""通过生产守护进程入口检查配置和启动副作用。"""
import copy
import json
import math
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request


DAEMON = Path(sys.argv.pop(1)).resolve()


def free_port(kind):
    with socket.socket(socket.AF_INET, kind) as connection:
        connection.bind(('127.0.0.1', 0))
        return connection.getsockname()[1]


class DaemonConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mozart-config-')
        self.root = Path(self.temporary.name)
        self.control_port = free_port(socket.SOCK_STREAM)
        self.audio_port = free_port(socket.SOCK_DGRAM)
        self.base = {
            'network': {'control': {'host': '127.0.0.1', 'port': self.control_port},
                        'audio': {'host': '127.0.0.1', 'port': self.audio_port}},
            'rvc': {'enabled': True, 'mock': {'generator': True, 'hubert': True, 'rmvpe': True},
                    'models_dir': 'new-models'},
            'storage': {'temp_dir': 'new-storage', 'presets_path': 'new-presets.json'},
        }

    def tearDown(self):
        self.temporary.cleanup()

    def changed(self, key, value):
        config = copy.deepcopy(self.base)
        current = config
        parts = key.split('.')
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = value
        return config

    def assert_rejected(self, config, key, value):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            self.check_rejected(Path(directory), config, key, value)

    def check_rejected(self, case_root, config, key, value):
        path = case_root/'config.yaml'
        path.write_text(config if isinstance(config, str) else json.dumps(config))
        result = subprocess.run([str(DAEMON), str(path)], cwd=case_root,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(key, result.stdout)
        self.assertIn(value, result.stdout)
        self.assertNotIn('RVC pipeline initialized', result.stdout)
        self.assertNotIn('HTTP API server listening', result.stdout)
        for name in ('new-models', 'new-storage', 'new-presets.json'):
            self.assertFalse((case_root/name).exists(), (name, result.stdout))
        for kind, port in ((socket.SOCK_STREAM, self.control_port),
                           (socket.SOCK_DGRAM, self.audio_port)):
            with socket.socket(socket.AF_INET, kind) as connection:
                connection.bind(('127.0.0.1', port))

    def test_invalid_types_ranges_and_structures_fail_before_side_effects(self):
        cases = [
            ('input.contract.sample_rate', -1), ('input.contract.sample_rate', 48000),
            ('output.sample_rate', 0), ('output.sample_rate', 16000),
            ('network.audio.frame_duration_ms', -20), ('network.audio.frame_duration_ms', 10),
            ('network.audio.port', -1), ('network.audio.port', 0), ('network.audio.port', 65536),
            ('network.control.port', -1), ('network.control.port', 65536),
            ('network.control.port', '18080'), ('network.audio.port', 1.5),
            ('rvc.pitch_shift', 'high'), ('rvc.pitch_shift', -13), ('rvc.pitch_shift', 13),
            ('rvc.filter_radius', -1), ('rvc.filter_radius', 8),
            ('rvc.index_rate', -0.1), ('rvc.index_rate', 1.1),
            ('rvc.index_rate', math.nextafter(1., math.inf)),
            ('rvc.index_rate', math.nextafter(0., -math.inf)),
            ('rvc.rms_mix_rate', -0.1), ('rvc.rms_mix_rate', 1.1),
            ('rvc.rms_mix_rate', math.nextafter(1., math.inf)),
            ('rvc.protect', -0.1), ('rvc.protect', 0.51), ('rvc.protect', 1e300),
            ('rvc.protect', math.nextafter(.5, math.inf)),
            ('rvc.f0_method', 'harvest'), ('rvc.device', 'automatic'),
            ('rvc.realtime_playout_delay_ms', -20), ('rvc.realtime_playout_delay_ms', 1020),
            ('rvc.realtime_playout_delay_ms', 21),
            ('storage.max_queue_depth', -1), ('storage.max_queue_depth', 0),
            ('storage.max_queue_depth', 2**64), ('storage.max_cache_size_mb', -1),
            ('storage.max_cache_size_mb', 1.5), ('storage.max_cache_size_mb', 2**64),
            ('storage.temp_dir', ''), ('storage.temp_dir', 12),
            ('storage.presets_path', []), ('storage.ffmpeg_path', ''),
            ('network.audio.host', '999.0.0.1'), ('network.control.host', ''),
            ('rvc.enabled', 'false'), ('rvc.half', 'no'), ('rvc.mock.generator', []),
            ('input.meta.vad_enabled', 'yes'), ('storage.file_denoise', None),
            ('rvc.hubert_path', None), ('rvc.rmvpe_path', []),
            ('rvc.realtime_hubert_path', False), ('rvc.realtime_rmvpe_path', {}),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                token = 'null' if value is None else str(value).lower() if isinstance(value, bool) else str(value)
                if value == []:
                    token = '[]'
                if value == {}:
                    token = '{}'
                if value == '':
                    token = key+'='
                self.assert_rejected(self.changed(key, value), key, token)
        for key, value in [('input', None), ('network.audio', []), ('storage', 'bad'), ('rvc.mock', False)]:
            with self.subTest(key=key):
                self.assert_rejected(self.changed(key, value), key, 'expected a mapping')
        config = self.changed('rvc.mock.generator', 'bad')
        config['rvc']['enabled'] = False
        self.assert_rejected(config, 'rvc.mock.generator', 'bad')

    def test_null_and_container_values_never_select_defaults(self):
        for key in ('rvc.pitch_shift', 'rvc.index_rate', 'rvc.enabled', 'rvc.models_dir',
                    'network.control.port', 'storage.max_cache_size_mb'):
            for value in (None, [], {}):
                with self.subTest(key=key, value=value):
                    self.assert_rejected(self.changed(key, value), key, 'expected a non-null scalar')

    def test_nonfinite_numbers_are_rejected(self):
        for key in ('rvc.index_rate', 'rvc.rms_mix_rate', 'rvc.protect'):
            for value in ('.nan', '.inf', '-.inf'):
                with self.subTest(key=key, value=value):
                    config = json.dumps(self.changed(key, '__NONFINITE__')).replace('"__NONFINITE__"', value)
                    self.assert_rejected(config, key, 'expected a finite number')

    def test_explicit_yaml_tags_match_field_types(self):
        for key, tagged in [
            ('rvc.pitch_shift', '!!float 1'),
            ('rvc.device', '!!int cpu'),
            ('rvc.pitch_shift', '!!str 1'),
            ('rvc.enabled', '!!str false'),
            ('rvc.enabled', '!!int 0'),
            ('rvc.index_rate', '!!bool true'),
            ('rvc.models_dir', '!!float new-models'),
            ('rvc.pitch_shift', '!custom 1'),
        ]:
            with self.subTest(key=key, tagged=tagged):
                config = json.dumps(self.changed(key, '__TAGGED__')).replace('"__TAGGED__"', tagged)
                self.assert_rejected(config, key, 'expected a scalar tagged as')
        fraction = json.dumps(self.changed('rvc.index_rate', '__TAGGED__')).replace('"__TAGGED__"', '!!int 0.5')
        self.assert_rejected(fraction, 'rvc.index_rate', 'expected an integer-tagged number')
        config = self.changed('rvc.enabled', '__BOOL__')
        config['rvc'].update(pitch_shift='__INT__', index_rate='__FLOAT__',
                             rms_mix_rate='__INT__', protect='__HALF__', device='__STRING__')
        rendered = json.dumps(config)
        for token, tagged in [('__BOOL__', '!!bool false'), ('__INT__', '!!int 1'),
                              ('__FLOAT__', '!!float 1'), ('__HALF__', '!!float 0.5'),
                              ('__STRING__', '!!str cpu')]:
            rendered = rendered.replace(json.dumps(token), tagged)
        self.assert_starts(rendered)

    def test_invalid_root_is_rejected(self):
        for contents in ('', 'null', '[]', 'true', 'broken'):
            with self.subTest(contents=contents):
                self.assert_rejected(contents, '<root>', 'expected a mapping')

    def test_missing_fields_use_defaults_and_disabled_rvc_starts(self):
        config = self.changed('rvc.enabled', False)
        del config['rvc']['mock']
        self.assert_starts(config)

    def test_legal_parameter_boundaries_start(self):
        for ratios, pitch, radius, delay in [
            ((0., 0., 0.), -12, 0, 0),
            ((1., 1., .5), 12, 7, 1000),
            ((math.nextafter(1., 0.), math.nextafter(1., 0.), math.nextafter(.5, 0.)), 0, 3, 400),
        ]:
            with self.subTest(ratios=ratios):
                config = self.changed('rvc.enabled', False)
                config['rvc'].update(index_rate=ratios[0], rms_mix_rate=ratios[1], protect=ratios[2],
                                     pitch_shift=pitch, filter_radius=radius, realtime_playout_delay_ms=delay)
                config['storage'].update(max_queue_depth=1, max_cache_size_mb=0)
                self.assert_starts(config)

    def assert_starts(self, config):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            self.check_starts(Path(directory), config)

    def check_starts(self, case_root, config):
        path = case_root/'config.yaml'
        path.write_text(config if isinstance(config, str) else json.dumps(config))
        with (case_root/'daemon.log').open('w+') as output:
            process = subprocess.Popen([str(DAEMON), str(path)], cwd=case_root,
                                       stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic()+5
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        output.seek(0)
                        self.fail(output.read())
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{self.control_port}/api/status', timeout=.2) as response:
                            status = json.load(response)
                        break
                    except (OSError, urllib.error.URLError):
                        time.sleep(.02)
                else:
                    self.fail('Daemon did not start')
                self.assertEqual(status['mode'], 'idle')
                self.assertFalse((case_root/'new-models').exists())
                self.assertTrue((case_root/'new-storage').is_dir())
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=3), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
