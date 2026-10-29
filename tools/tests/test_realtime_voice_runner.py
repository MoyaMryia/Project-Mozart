"""tools/run-realtime-voice.sh pre-restart 自测：字幕目标持久化与就绪判定。"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO/'tools'/'run-realtime-voice.sh'
sys.path.insert(0, str(REPO/'usb-audio'))
import proto

CAPTURE = 'plughw:CARD=MIC,DEV=0'
PLAYBACK = 'plughw:CARD=Device,DEV=0'
BRIDGE_CAPTURE = 'plughw:CARD=Loopback,DEV=1,SUBDEV=0'
BRIDGE_PLAYBACK = 'plughw:CARD=Loopback,DEV=0,SUBDEV=1'
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

# arecord/aplay -l 输出 card 表，-L 输出设备名清单。两者由同一份 id 清单推导。
FAKE_ALSA = '''#!/usr/bin/env bash
case "$1" in
    -l) cat <<'TABLE'
{tables}
TABLE
        ;;
    *) cat <<'DEVICES'
{devices}
DEVICES
        ;;
esac
'''


def alsa_fixture(ids):
    rows, names = [], []
    for identifier in ids:
        if identifier == 'null':
            names.append('null')
            continue
        match = re.match(r'plughw:CARD=(\w+),DEV=(\d+)', identifier)
        short, device = match.group(1), match.group(2)
        rows.append(f'card {len(rows)}: {short} [{short}], device {device}: {short} PCM [{short} PCM]')
        names.append(identifier)
    return FAKE_ALSA.format(tables='\n'.join(rows), devices='\n'.join(names))


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
        self.write_executable(self.bin/'arecord', alsa_fixture([
            CAPTURE, 'plughw:CARD=MIC,DEV=1', 'plughw:CARD=Loopback,DEV=1']))
        self.write_executable(self.bin/'aplay', alsa_fixture([
            PLAYBACK, 'plughw:CARD=Device,DEV=1', 'plughw:CARD=Loopback,DEV=0', 'null']))
        self.run_dir = self.root/'run'
        self.run_dir.mkdir()
        self.pre_log = self.run_dir/'pre-argv.log'
        self.status_file = self.run_dir/'pre-status.json'
        self.previous_file = self.run_dir/'pre-status.json.previous'
        self.devices_file = self.run_dir/'pre-devices.json'
        self.bridge_status = self.run_dir/'bridge-status.json'

    def seed_bridge_status(self, active=True, age=0.0, **overrides):
        snapshot = {'role': 'jetson', 'active': active, 'sent': 10, 'received': 20,
                    'dropped': 3, 'errors': 1, 'last_error': '',
                    'updated_at': time.time() - age}
        snapshot.update(overrides)
        self.bridge_status.write_text(json.dumps(snapshot))

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

    def run_pre_restart(self, *, env_target=None, write_status=True, exit_code=0, status_pid=None,
                        output=None, reference=None, capture=None, playback=None):
        """用真实 bash 执行 pre-restart。env_target=None 表示不设置 SUBTITLE_TARGET。"""
        env = {
            'PATH': str(self.bin) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'),
            'HOME': str(self.root),
            'LANG': 'C.UTF-8',
            'RUN_DIR': str(self.run_dir),
            'READY_TIMEOUT': '1',
            'MOZART_STATUS_FILE': str(self.bridge_status),
            'FAKE_PRE_LOG': str(self.pre_log),
            'FAKE_PRE_WRITE': '1' if write_status else '0',
            'FAKE_PRE_EXIT': str(exit_code),
        }
        if env_target is not None:
            env['SUBTITLE_TARGET'] = env_target
        if status_pid is not None:
            env['FAKE_PRE_PID'] = str(status_pid)
        if output is not None:
            env['OUTPUT'] = output
        if reference is not None:
            env['REFERENCE'] = reference
        self.pre_log.unlink(missing_ok=True)
        argv = ['bash', str(self.script), 'pre-restart']
        if capture is not None:
            argv += ['--capture', capture]
        if playback is not None:
            argv += ['--playback', playback]
        return subprocess.run(argv, capture_output=True, text=True, timeout=10, env=env)

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
            # 指向临时文件，避免板上真实桥状态影响入口校验。
            'MOZART_STATUS_FILE': str(self.bridge_status),
        }
        env.update(extra_env)
        return subprocess.run(['bash', str(self.script), 'start'],
                              capture_output=True, text=True, timeout=20, env=env)

    def prepare_start_dependencies(self, playback):
        self.write_executable(self.root/'build-gpu'/'state'/'mozart_stated', '#!/bin/sh\n')
        self.write_executable(self.root/'build'/'usb-pre'/'mozart-pre', '#!/bin/sh\n')
        config = self.root/'backend.yaml'
        config.write_text('storage_dir: /tmp\n')
        self.write_executable(self.bin/'arecord', alsa_fixture([
            CAPTURE, 'plughw:CARD=MIC,DEV=1', 'plughw:CARD=Loopback,DEV=1']))
        self.write_executable(self.bin/'aplay', alsa_fixture([playback, 'null']))
        return config

    def test_output_must_be_voice_speech_or_both(self):
        config = self.prepare_start_dependencies(PLAYBACK)

        result = self.run_start(OUTPUT='mixed', PLAYBACK=PLAYBACK, CONFIG=str(config))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('OUTPUT 必须是 voice、speech 或 both', result.stderr)

    def test_both_forwards_full_mode_and_allows_degraded_speech(self):
        argv = self.captured_speech_start(output='both')
        self.assertEqual(argv[argv.index('--mode') + 1], 'full')
        self.assertEqual(argv[argv.index('--playback-device') + 1], PLAYBACK)
        self.assertIn('--allow-degraded', argv)

    def test_voice_does_not_start_speech_service(self):
        argv = self.captured_speech_start(output='voice')
        self.assertEqual(argv[argv.index('--mode') + 1], 'captions')
        self.assertNotIn('--allow-degraded', argv)

    def test_devices_offers_null_playback(self):
        self.write_executable(self.bin/'aplay', alsa_fixture([
            'plughw:CARD=Device,DEV=0', 'null']))
        self.write_executable(self.bin/'arecord', alsa_fixture(['plughw:CARD=MIC,DEV=0']))

        devices = self.run_devices()

        playback = [item['id'] for item in devices['playback']]
        # 真实声卡必须仍被解出来；只断言 null 会掩盖解析失败。
        self.assertEqual(playback[0], 'plughw:CARD=Device,DEV=0')
        self.assertEqual(playback[-1], 'null')

    def run_devices(self):
        env = {
            'PATH': str(self.bin) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'),
            'HOME': str(self.root),
            'LANG': 'C.UTF-8',
            'MOZART_STATUS_FILE': str(self.bridge_status),
        }
        result = subprocess.run(['bash', str(self.script), 'devices'],
                                capture_output=True, text=True, timeout=20, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_devices_adds_the_bridge_pair_in_its_own_group(self):
        self.seed_bridge_status()

        devices = self.run_devices()

        capture = {item['id']: item for item in devices['capture']}
        playback = {item['id']: item for item in devices['playback']}
        self.assertEqual(capture[BRIDGE_CAPTURE]['group'], 'usb')
        self.assertEqual(playback[BRIDGE_PLAYBACK]['group'], 'usb')
        self.assertEqual(capture[CAPTURE]['group'], 'local')

    def test_devices_omits_the_pair_without_a_loopback_card(self):
        # 没有 snd-aloop 时不能提供打不开的协议项。
        self.write_executable(self.bin/'arecord', alsa_fixture([CAPTURE]))
        self.write_executable(self.bin/'aplay', alsa_fixture([PLAYBACK, 'null']))

        devices = self.run_devices()

        self.assertEqual([item['id'] for item in devices['capture']], [CAPTURE])
        self.assertEqual([item['id'] for item in devices['playback']], [PLAYBACK, 'null'])

    def test_devices_hides_raw_loopback_devices(self):
        self.seed_bridge_status()

        devices = self.run_devices()

        ids = [item['id'] for item in devices['capture'] + devices['playback']]
        self.assertNotIn('plughw:CARD=Loopback,DEV=0', ids)
        self.assertNotIn('plughw:CARD=Loopback,DEV=1', ids)

    def test_devices_exposes_bridge_counters(self):
        self.seed_bridge_status()

        bridge = self.run_devices()['bridge']

        self.assertTrue(bridge['active'])
        self.assertEqual((bridge['received'], bridge['sent'], bridge['dropped']), (20, 10, 3))
        self.assertIn('age_seconds', bridge)

    def test_devices_reports_a_stale_bridge_as_inactive(self):
        self.seed_bridge_status(age=120)

        bridge = self.run_devices()['bridge']

        self.assertFalse(bridge['active'])
        self.assertIn('刷新窗口', bridge['reason'])

    def test_devices_reports_a_missing_status_file(self):
        bridge = self.run_devices()['bridge']

        self.assertFalse(bridge['active'])
        self.assertIn('读取桥状态失败', bridge['reason'])

    def test_pre_restart_accepts_the_bridge_pair_when_the_bridge_runs(self):
        self.seed_devices()
        self.seed_bridge_status()

        result = self.run_pre_restart(capture=BRIDGE_CAPTURE, playback=BRIDGE_PLAYBACK)

        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.pre_argv()
        self.assertEqual(argv[argv.index('-d') + 1], BRIDGE_CAPTURE)
        self.assertEqual(argv[argv.index('-o') + 1], BRIDGE_PLAYBACK)

    def test_pre_restart_refuses_a_bridge_device_without_a_running_bridge(self):
        self.seed_devices()
        self.seed_bridge_status(active=False)

        result = self.run_pre_restart(capture=BRIDGE_CAPTURE, playback=BRIDGE_PLAYBACK)

        self.assertEqual(result.returncode, 1)
        self.assertIn('USB 桥不可用', result.stderr)
        self.assertIn('USB 链路未就绪', result.stderr)
        self.assertFalse(self.pre_log.exists())

    def test_pre_restart_needs_the_bridge_for_a_mixed_pair(self):
        self.seed_devices()

        result = self.run_pre_restart(capture=CAPTURE, playback=BRIDGE_PLAYBACK)

        self.assertEqual(result.returncode, 1)
        self.assertIn('USB 桥不可用', result.stderr)

    def test_pre_restart_reports_a_stale_bridge_status(self):
        self.seed_devices()
        self.seed_bridge_status(age=120)

        result = self.run_pre_restart(capture=BRIDGE_CAPTURE, playback=BRIDGE_PLAYBACK)

        self.assertEqual(result.returncode, 1)
        self.assertIn('刷新窗口', result.stderr)

    def test_pre_restart_reports_a_missing_status_file(self):
        self.seed_devices()

        result = self.run_pre_restart(capture=BRIDGE_CAPTURE, playback=BRIDGE_PLAYBACK)

        self.assertEqual(result.returncode, 1)
        self.assertIn('读取桥状态失败', result.stderr)

    def test_pre_restart_treats_the_plain_loopback_id_as_the_bridge(self):
        # 同一通路的等价写法也必须触发桥检查。
        self.seed_devices()

        result = self.run_pre_restart(capture='plughw:CARD=Loopback,DEV=1',
                                      playback=BRIDGE_PLAYBACK)

        self.assertEqual(result.returncode, 1)
        self.assertIn('USB 桥不可用', result.stderr)

    def test_pre_restart_refuses_a_device_that_does_not_exist(self):
        self.seed_devices()

        result = self.run_pre_restart(capture='plughw:CARD=NOPE,DEV=0', playback=PLAYBACK)

        self.assertEqual(result.returncode, 1)
        self.assertIn('采集设备不存在', result.stderr)
        self.assertFalse(self.pre_log.exists())

    def test_bridge_status_default_matches_the_protocol_module(self):
        match = re.search(r'BRIDGE_STATUS=\$\{MOZART_STATUS_FILE:-([^}]*)\}',
                          SCRIPT.read_text())
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), proto.STATUS_FILE)

    def test_pre_restart_keeps_requested_device_in_both_mode(self):
        # both 加参考音色时启动输出是 null，但控制面切换必须保留传入的设备。
        self.seed_devices()

        result = self.run_pre_restart(output='both', reference='/tmp/reference.wav')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pre_argv()[self.pre_argv().index('-o') + 1], PLAYBACK)
        devices = json.loads(self.devices_file.read_text())
        self.assertEqual(devices['playback'], PLAYBACK)
        self.assertIn('"playback":"%s"' % PLAYBACK, result.stdout)

    def test_start_needs_the_bridge_for_a_protocol_output(self):
        # start 与 pre-restart 共用同一道桥检查；这里钉住 start 的调用点。
        config = self.prepare_start_dependencies(PLAYBACK)
        self.write_executable(self.bin/'aplay', alsa_fixture([
            PLAYBACK, 'plughw:CARD=Loopback,DEV=0', 'null']))

        result = self.run_start(OUTPUT='voice', PLAYBACK=BRIDGE_PLAYBACK, CONFIG=str(config))

        self.assertEqual(result.returncode, 1)
        self.assertIn('USB 桥不可用', result.stderr)
        self.assertIn('读取桥状态失败', result.stderr)

    def test_speech_requires_an_existing_reference(self):
        config = self.prepare_start_dependencies(PLAYBACK)

        result = self.run_start(OUTPUT='speech', PLAYBACK=PLAYBACK, CONFIG=str(config),
                                REFERENCE=str(self.root/'missing.wav'))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('参考音频不存在', result.stderr)

    def test_speech_early_audio_requires_a_boolean_switch(self):
        config = self.prepare_start_dependencies(PLAYBACK)
        result = self.run_start(OUTPUT='speech', CONFIG=str(config), SPEECH_EARLY_AUDIO='yes')
        self.assertEqual(result.returncode, 1)
        self.assertIn('SPEECH_EARLY_AUDIO 必须是 0 或 1', result.stderr)

    def captured_speech_start(self, output='speech', **settings):
        config = self.prepare_start_dependencies(PLAYBACK)
        argv_file = self.root/'stack-argv.json'
        fake_python = self.root/'fake-python'
        self.write_executable(fake_python, '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
Path(os.environ['FAKE_STACK_ARGV']).write_text(json.dumps(sys.argv[1:]))
sys.exit(9)
''')
        self.write_executable(self.bin/'curl', '#!/bin/sh\nexit 1\n')
        result = self.run_start(OUTPUT=output, CONFIG=str(config), PYTHON=str(fake_python),
                                FAKE_STACK_ARGV=str(argv_file), **settings)
        self.assertEqual(result.returncode, 1)
        self.assertIn('未就绪', result.stderr)
        return json.loads(argv_file.read_text())

    def test_speech_forwards_early_audio_and_runtime_path_without_splitting(self):
        runtime_path = self.root/'runtime package'/'python'
        runtime_path.mkdir(parents=True)
        runtime = str(runtime_path)
        argv = self.captured_speech_start(SPEECH_EARLY_AUDIO='1', TTS_PYTHONPATH=runtime)
        self.assertIn('--early-audio', argv)
        self.assertEqual(argv[argv.index('--tts-pythonpath')+1], runtime)

    def test_speech_missing_runtime_directory_fails_before_start(self):
        config = self.prepare_start_dependencies(PLAYBACK)
        result = self.run_start(OUTPUT='speech', CONFIG=str(config),
                                TTS_PYTHONPATH=str(self.root/'missing-runtime'))
        self.assertEqual(result.returncode, 1)
        self.assertIn('TTS_PYTHONPATH 目录不存在', result.stderr)

    def test_speech_runtime_relative_directory_is_resolved_once(self):
        runtime = self.root/'runtime package'/'python'
        runtime.mkdir(parents=True)
        argv = self.captured_speech_start(TTS_PYTHONPATH=os.path.relpath(runtime))
        self.assertEqual(argv[argv.index('--tts-pythonpath')+1], str(runtime))

    def test_speech_default_retains_complete_piece_output(self):
        argv = self.captured_speech_start()
        self.assertNotIn('--early-audio', argv)
        self.assertNotIn('--tts-pythonpath', argv)

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
