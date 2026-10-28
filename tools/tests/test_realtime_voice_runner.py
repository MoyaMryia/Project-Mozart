"""tools/run-realtime-voice.sh pre-restart 自测：字幕目标持久化与就绪判定。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO/'tools'/'run-realtime-voice.sh'

CAPTURE = 'plughw:CARD=MIC,DEV=0'
PLAYBACK = 'plughw:CARD=Device,DEV=0'
MIXING = 'mozartout'
SAVED_TARGET = '127.0.0.1:18100'
OVERRIDE_TARGET = '127.0.0.1:18999'

# 记录 argv。FAKE_PRE_WRITE=0 时不写状态，FAKE_PRE_EXIT 控制退出码。
FAKE_PRE = '''#!/usr/bin/env bash
set -u
printf '%s\\n' "$@" >> "$FAKE_PRE_LOG"
status=
while [ $# -gt 0 ]; do
    case "$1" in
        --status-file) status=$2; shift 2 ;;
        *) shift ;;
    esac
done
if [ "${FAKE_PRE_WRITE:-1}" = 1 ] && [ -n "$status" ]; then
    printf '{"state":"ready","pid":%s,"generation":"%s"}\\n' "${FAKE_PRE_PID:-$$}" "${FAKE_PRE_GENERATION:-2}" > "$status"
fi
exit "${FAKE_PRE_EXIT:-0}"
'''

# arecord/aplay 的 -L 输出只有 fixture 设备，脚本不会访问真实声卡。
FAKE_ALSA = '''#!/usr/bin/env bash
cat <<'DEVICES'
{devices}
DEVICES
'''


class RealtimeVoiceRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mozart-realtime-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        tools = self.root/'tools'
        tools.mkdir()
        self.script = tools/'run-realtime-voice.sh'
        shutil.copy2(SCRIPT, self.script)
        self.write_executable(self.root/'build'/'usb-pre'/'mozart-pre', FAKE_PRE)
        self.bin = self.root/'bin'
        self.write_executable(self.bin/'arecord', FAKE_ALSA.format(
            devices=f'{CAPTURE}\nplughw:CARD=MIC,DEV=1'))
        self.write_executable(self.bin/'aplay', FAKE_ALSA.format(
            devices=f'{PLAYBACK}\nplughw:CARD=Device,DEV=1'))
        self.run_dir = self.root/'run'
        self.run_dir.mkdir()
        self.pre_log = self.run_dir/'pre-argv.log'
        self.status_file = self.run_dir/'pre-status.json'
        self.previous_file = self.run_dir/'pre-status.json.previous'
        self.devices_file = self.run_dir/'pre-devices.json'

    def write_executable(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)

    def seed_devices(self, target=SAVED_TARGET, legacy=False):
        devices = {'capture': CAPTURE, 'playback': PLAYBACK}
        if not legacy:
            devices['subtitle_target'] = target
        self.devices_file.write_text(json.dumps(devices))

    def seed_stale_status(self):
        self.status_file.write_text(json.dumps({'state': 'ready', 'generation': '1'}))

    def run_pre_restart(self, *, env_target=None, write_status=True, exit_code=0, status_pid=None):
        """用真实 bash 执行 pre-restart。env_target=None 表示不设置 SUBTITLE_TARGET。"""
        env = {
            'PATH': str(self.bin) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'),
            'HOME': str(self.root),
            'LANG': 'C.UTF-8',
            'RUN_DIR': str(self.run_dir),
            'READY_TIMEOUT': '1',
            'FAKE_PRE_LOG': str(self.pre_log),
            'FAKE_PRE_WRITE': '1' if write_status else '0',
            'FAKE_PRE_EXIT': str(exit_code),
        }
        if env_target is not None:
            env['SUBTITLE_TARGET'] = env_target
        if status_pid is not None:
            env['FAKE_PRE_PID'] = str(status_pid)
        self.pre_log.unlink(missing_ok=True)
        return subprocess.run(['bash', str(self.script), 'pre-restart'],
                              capture_output=True, text=True, timeout=10, env=env)

    def pre_argv(self):
        return self.pre_log.read_text().splitlines()

    def assert_subtitle_arg(self, argv, expected):
        if expected is None:
            self.assertNotIn('-b', argv)
            return
        self.assertIn('-b', argv)
        self.assertEqual(argv[argv.index('-b') + 1], expected)

    def test_restores_saved_target_and_records_devices(self):
        self.seed_devices()
        self.seed_stale_status()

        result = self.run_pre_restart()

        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.pre_argv()
        self.assertEqual(argv[argv.index('-d') + 1], CAPTURE)
        self.assertEqual(argv[argv.index('-o') + 1], PLAYBACK)
        self.assert_subtitle_arg(argv, SAVED_TARGET)
        devices = json.loads(self.devices_file.read_text())
        self.assertEqual(devices['subtitle_target'], SAVED_TARGET)
        self.assertIn('"status":"active"', result.stdout)

    def test_old_devices_without_target_start_without_subtitle(self):
        self.seed_devices(legacy=True)

        result = self.run_pre_restart()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_subtitle_arg(self.pre_argv(), None)
        devices = json.loads(self.devices_file.read_text())
        self.assertEqual(devices['subtitle_target'], '')

    def run_start(self, **extra_env):
        """用空壳依赖执行 start，只验证入口校验。"""
        env = {
            'PATH': str(self.bin) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'),
            'HOME': str(self.root),
            'LANG': 'C.UTF-8',
            'RUN_DIR': str(self.run_dir),
            'READY_TIMEOUT': '1',
            'PYTHON': '/bin/true',
        }
        env.update(extra_env)
        return subprocess.run(['bash', str(self.script), 'start'],
                              capture_output=True, text=True, timeout=20, env=env)

    def prepare_start_dependencies(self, playback):
        self.write_executable(self.root/'build-gpu'/'state'/'mozart_stated', '#!/bin/sh\n')
        self.write_executable(self.root/'build'/'usb-pre'/'mozart-pre', '#!/bin/sh\n')
        config = self.root/'backend.yaml'
        config.write_text('storage_dir: /tmp\n')
        self.write_executable(self.bin/'arecord', FAKE_ALSA.format(
            devices=f'{CAPTURE}\nplughw:CARD=MIC,DEV=1'))
        self.write_executable(self.bin/'aplay', FAKE_ALSA.format(devices=playback))
        return config

    def test_speech_rejects_direct_hardware_playback(self):
        config = self.prepare_start_dependencies(PLAYBACK)

        result = self.run_start(SPEECH='1', PLAYBACK=PLAYBACK, CONFIG=str(config))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('播报需要混音 PCM', result.stderr)

    def test_speech_accepts_a_mixing_device(self):
        config = self.prepare_start_dependencies(MIXING)

        result = self.run_start(SPEECH='1', PLAYBACK=MIXING, CONFIG=str(config),
                                REFERENCE=str(self.root/'missing.wav'))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('参考音频不存在', result.stderr)

    def test_environment_target_overrides_saved_target(self):
        self.seed_devices()

        result = self.run_pre_restart(env_target=OVERRIDE_TARGET)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_subtitle_arg(self.pre_argv(), OVERRIDE_TARGET)
        devices = json.loads(self.devices_file.read_text())
        self.assertEqual(devices['subtitle_target'], OVERRIDE_TARGET)

    def test_empty_environment_target_disables_subtitle(self):
        self.seed_devices()

        result = self.run_pre_restart(env_target='')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_subtitle_arg(self.pre_argv(), None)
        devices = json.loads(self.devices_file.read_text())
        self.assertEqual(devices['subtitle_target'], '')

    def test_stale_ready_status_moves_to_previous_before_new_status(self):
        self.seed_devices()
        self.seed_stale_status()

        result = self.run_pre_restart()

        self.assertEqual(result.returncode, 0, result.stderr)
        previous = json.loads(self.previous_file.read_text())
        current = json.loads(self.status_file.read_text())
        self.assertEqual(previous['generation'], '1')
        self.assertEqual(current['generation'], '2')

    def test_missing_new_status_fails_with_stale_ready_present(self):
        self.seed_devices()
        self.seed_stale_status()

        result = self.run_pre_restart(write_status=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('未就绪', result.stderr)
        self.assertFalse(self.status_file.exists())
        previous = json.loads(self.previous_file.read_text())
        self.assertEqual(previous, {'state': 'ready', 'generation': '1'})
        self.assert_subtitle_arg(self.pre_argv(), SAVED_TARGET)

    def test_another_process_ready_status_does_not_admit_startup(self):
        self.seed_devices()
        result = self.run_pre_restart(status_pid=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('未就绪', result.stderr)

    def test_pre_exit_without_new_status_fails_within_timeout(self):
        self.seed_devices()
        self.seed_stale_status()

        result = self.run_pre_restart(write_status=False, exit_code=3)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('未就绪', result.stderr)
        self.assertFalse(self.status_file.exists())
        self.assertTrue(self.previous_file.exists())


if __name__ == '__main__':
    unittest.main()
