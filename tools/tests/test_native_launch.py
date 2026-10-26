"""原生启动器的无硬件测试。隔离仓库和命令替身位于 build/run 下。"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ('run-native-host.sh', 'run-native-pre.sh', 'native-launch-common.sh')
MOCK_COMMAND = r'''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['MOCK_EVENTS'], 'a') as f:
    f.write(json.dumps([name, *args]) + '\n')
if name == 'sudo':
    assert args.pop(0) == '-n'
    os.execvp(args[0], args)
if name == 'chrt':
    assert args[:2] == ['--fifo', '40']
    os.execvp(args[2], args[2:])
if name == 'test':
    assert args[0] == '-e' and len(args) == 2
    endpoint = Path(args[1]).name
    assert args[1] == '/dev/ffs-mozart/' + endpoint
    assert endpoint in ('ep1', 'ep2', 'ep3', 'ep4')
    sys.exit(1 if endpoint == os.environ.get('MOCK_PRE_MISSING') else 0)
if name == 'fuser':
    assert len(args) == 1
    endpoint = Path(args[0]).name
    assert args[0] == '/dev/ffs-mozart/' + endpoint
    assert endpoint in ('ep1', 'ep2', 'ep3', 'ep4')
    if os.environ.get('MOCK_PRE_FUSER_ERROR'):
        sys.exit(2)
    busy = endpoint in ('ep1', 'ep2') or endpoint == os.environ.get('MOCK_PRE_BUSY')
    sys.exit(0 if busy else 1)
if name == 'python3':
    assert args == ['-c', 'import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind(("127.0.0.1",19040))']
    if os.environ.get('MOCK_PRE_PORT_BUSY'):
        print('OSError: [Errno 98] Address already in use', file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
if name == 'systemctl':
    if args[0] == 'is-active':
        state = os.environ.get('LEGACY_STATE', 'active')
        print(state)
        sys.exit(0 if state == 'active' else 3)
    if args[0] == 'start' and os.environ.get('RESTORE_FAIL'):
        sys.exit(5)
if name == 'pactl':
    if args[0] == 'get-default-sink': print('test-sink')
    elif args[:3] == ['list', 'short', 'sinks']:
        print('1\ttest-sink\tdriver\tformat\t' + os.environ.get('SINK_STATE', 'RUNNING'))
'''
NATIVE_C = r'''
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static volatile sig_atomic_t stop;
static void on_stop(int sig) { stop = sig; }
int main(int argc, char **argv) {
    FILE *f = fopen(getenv("MOCK_EVENTS"), "a");
    fprintf(f, "[\"native\"");
    for (int i=1; i<argc; ++i) fprintf(f, ",\"%s\"", argv[i]);
    fprintf(f, "]\n"); fclose(f);
    if (getenv("NATIVE_FAIL")) return atoi(getenv("NATIVE_FAIL"));
    signal(SIGTERM, on_stop); signal(SIGINT, on_stop);
    for (int i=1; i+1<argc; ++i) if (!strcmp(argv[i], "--status-file")) {
        f=fopen(argv[i+1], "w"); fprintf(f, "{\"state\":\"ready\"}\n"); fclose(f);
    }
    while (!stop) usleep(10000);
    f=fopen(getenv("MOCK_EVENTS"), "a"); fprintf(f, "[\"native\",\"STOP/drain\"]\n"); fclose(f);
    return 0;
}
'''


class NativeLaunchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = ROOT / 'build' / 'run'
        cls.base.mkdir(parents=True, exist_ok=True)
        cls.compiled = tempfile.TemporaryDirectory(prefix='launch-compiler-', dir=cls.base)
        source = Path(cls.compiled.name) / 'native.c'
        source.write_text(NATIVE_C)
        cls.native = source.with_suffix('')
        subprocess.run(['cc', str(source), '-o', str(cls.native)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.compiled.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='launch-test-', dir=self.base)
        self.root = Path(self.temp.name)
        (self.root / 'usb-audio').mkdir()
        for name in SCRIPTS:
            shutil.copy2(ROOT / 'usb-audio' / name, self.root / 'usb-audio' / name)
        self.run_dir = self.root / 'build/run/usb-native-host'
        bindir = self.root / 'build/native-host'
        bindir.mkdir(parents=True)
        self.bin = bindir / 'mozart-usb-host'
        shutil.copy2(self.native, self.bin)
        self.pre_run_dir = self.root / 'build/run/usb-native-pre'
        pre_bin = self.root / 'build/usb-pre/mozart-pre'
        pre_bin.parent.mkdir(parents=True)
        shutil.copy2(self.native, pre_bin)
        self.mockdir = self.root / 'mocks'
        self.mockdir.mkdir()
        for name in ('sudo', 'test', 'fuser', 'python3', 'systemctl', 'pactl', 'cmake', 'chrt'):
            path = self.mockdir / name
            path.write_text(MOCK_COMMAND)
            path.chmod(0o755)
        self.events_path = self.root / 'events.jsonl'
        self.env = dict(os.environ, MOCK_EVENTS=str(self.events_path),
                        PATH=str(self.mockdir) + os.pathsep + os.environ['PATH'])
        for name in ('MOZART_CAPTURE', 'MOZART_PLAYBACK', 'MOZART_PULSE_SINK',
                     'MOZART_PRE_PLAYBACK', 'MOCK_PRE_MISSING', 'MOCK_PRE_BUSY',
                     'MOCK_PRE_PORT_BUSY', 'MOCK_PRE_FUSER_ERROR', 'NATIVE_FAIL', 'RESTORE_FAIL',
                     'LEGACY_STATE', 'SINK_STATE'):
            self.env.pop(name, None)
        self.processes = []

    def tearDown(self):
        try:
            try:
                for role, run_dir in (('host', self.run_dir), ('pre', self.pre_run_dir)):
                    if (run_dir / 'native.pid').exists():
                        self.call(role, 'stop')
            finally:
                for process in self.processes:
                    if process.poll() is None:
                        process.send_signal(signal.SIGTERM)
                    try:
                        process.communicate(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)
                        raise
        finally:
            self.temp.cleanup()

    def call(self, role, *args, check=True):
        return subprocess.run([str(self.root / 'usb-audio' / f'run-native-{role}.sh'), *args],
                              cwd=self.root, env=self.env, text=True, capture_output=True,
                              timeout=25, check=check)

    def events(self):
        if not self.events_path.exists():
            return []
        return [json.loads(line) for line in self.events_path.read_text().splitlines()]

    def spawn(self, *args, role='host'):
        run_dir = self.root / 'build/run' / f'usb-native-{role}'
        process = subprocess.Popen([str(self.root / 'usb-audio' / f'run-native-{role}.sh'), 'run', *args],
                                   cwd=self.root, env=self.env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (run_dir / 'status.json').exists():
                return process
            if process.poll() is not None:
                self.fail(process.communicate())
            time.sleep(0.02)
        self.fail('原生替身启动超时。')

    def assert_pre_isolated(self):
        events = self.events()
        self.assertFalse(any(event[0] in ('systemctl', 'pactl') for event in events))
        self.assertFalse(any(event[:3] in (['sudo', '-n', 'systemctl'],
                                         ['sudo', '-n', 'pactl']) for event in events))
        self.assertFalse((self.pre_run_dir / 'legacy.restore').exists())
        self.assertFalse((self.pre_run_dir / 'pulse.restore').exists())

    def assert_pre_probes(self, endpoints, *, missing=False, port=True):
        probes = []
        for endpoint in endpoints:
            path = '/dev/ffs-mozart/' + endpoint
            probes.extend([['sudo', '-n', 'test', '-e', path], ['test', '-e', path]])
            if not (missing and endpoint == endpoints[-1]):
                probes.extend([['sudo', '-n', 'fuser', path], ['fuser', path]])
        events = self.events()
        self.assertEqual([event for event in events if event[0] in ('test', 'fuser')
                          or event[:3] in (['sudo', '-n', 'test'],
                                          ['sudo', '-n', 'fuser'])], probes)
        queries = [event for event in events if event[0] == 'python3']
        self.assertEqual(len(queries), int(port))
        if port:
            self.assertEqual(queries[0][1], '-c')
            self.assertIn('s.bind(("127.0.0.1",19040))', queries[0][2])

    def assert_pre_rejected(self, message):
        result = self.call('pre', 'run', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)
        self.assertFalse(any(event[0] == 'native' or event[:3] == ['sudo', '-n', 'sh']
                             for event in self.events()))
        self.assertFalse((self.pre_run_dir / 'native.pid').exists())
        self.assertIn('process=stopped', self.call('pre', 'status').stdout)
        self.assert_pre_isolated()

    def test_pre_run_status_stop_use_dedicated_endpoints_without_playback(self):
        process = self.spawn(role='pre')
        status = self.call('pre', 'status').stdout
        self.assertIn('process=running', status)
        self.assertIn(str(self.root / 'build/usb-pre/mozart-pre'), status)
        self.assertIn('"state":"ready"', status)
        self.assertEqual(self.call('pre', 'stop').returncode, 0)
        self.assertEqual(process.wait(timeout=5), 0)
        events = self.events()
        native = next(event for event in events if event[0] == 'native')
        self.assertEqual(native, ['native', '--usb-endpoint', '/dev/ffs-mozart',
                                 '--mode', 'direct', '--status-file',
                                 str(self.pre_run_dir / 'status.json'), '--ctl-port', '19040'])
        self.assertNotIn('-o', native)
        self.assertIn(['native', 'STOP/drain'], events)
        self.assertFalse((self.pre_run_dir / 'native.pid').exists())
        self.assertIn('process=stopped', self.call('pre', 'status').stdout)
        self.assert_pre_probes(['ep3', 'ep4'])
        self.assert_pre_isolated()

    def test_pre_explicit_playback_and_cli_arguments_reach_native(self):
        self.env['MOZART_PRE_PLAYBACK'] = 'test playback device'
        args = ['-n', '4', '--period-frames', '128', '--fade-ms', '5',
                '--no-denoise', '--gate', '--agc', '--limiter']
        process = self.spawn(*args, role='pre')
        self.call('pre', 'stop')
        self.assertEqual(process.wait(timeout=5), 0)
        native = next(event for event in self.events() if event[0] == 'native')
        self.assertEqual(native[1:1 + len(args)], args)
        self.assertEqual(native[-2:], ['-o', 'test playback device'])
        self.assertEqual(native.count('-o'), 1)
        self.assert_pre_probes(['ep3', 'ep4'])
        self.assert_pre_isolated()

    def test_pre_busy_endpoint_fails_before_native_start(self):
        for endpoint, probes in (('ep3', ['ep3']), ('ep4', ['ep3', 'ep4'])):
            with self.subTest(endpoint=endpoint):
                self.events_path.unlink(missing_ok=True)
                self.env['MOCK_PRE_BUSY'] = endpoint
                self.assert_pre_rejected('专用 USB 端点已被其它进程占用：/dev/ffs-mozart/' + endpoint)
                self.assert_pre_probes(probes, port=False)

    def test_pre_missing_endpoint_fails_before_native_start(self):
        for endpoint, probes in (('ep3', ['ep3']), ('ep4', ['ep3', 'ep4'])):
            with self.subTest(endpoint=endpoint):
                self.events_path.unlink(missing_ok=True)
                self.env['MOCK_PRE_MISSING'] = endpoint
                self.assert_pre_rejected('专用 USB 端点不存在：/dev/ffs-mozart/' + endpoint)
                self.assert_pre_probes(probes, missing=True, port=False)

    def test_pre_endpoint_query_error_fails_before_native_start(self):
        self.env['MOCK_PRE_FUSER_ERROR'] = '1'
        self.assert_pre_rejected('USB 端点占用查询失败：/dev/ffs-mozart/ep3 (2)')
        self.assert_pre_probes(['ep3'], port=False)

    def test_pre_busy_control_port_fails_before_native_start(self):
        self.env['MOCK_PRE_PORT_BUSY'] = '1'
        self.assert_pre_rejected('Address already in use')
        self.assert_pre_probes(['ep3', 'ep4'])

    def test_help_and_unknown_arguments_do_not_probe_hardware(self):
        for role in ('host', 'pre'):
            self.assertEqual(self.call(role, '--help').returncode, 0)
            self.assertNotEqual(self.call(role, 'run', '--status-file', '/tmp/status', check=False).returncode, 0)
            self.assertNotEqual(self.call(role, 'stop', 'extra', check=False).returncode, 0)
        self.assertEqual(self.events(), [])

    def test_shell_syntax(self):
        for name in SCRIPTS:
            subprocess.run(['bash', '-n', str(self.root / 'usb-audio' / name)], check=True,
                           capture_output=True)

    def test_argument_forwarding_does_not_evaluate_shell_text(self):
        input_name = "input ' $(touch injected) ;.wav"
        self.env.update(MOZART_CAPTURE='test capture', MOZART_PLAYBACK='test playback')
        process = self.spawn('--input', input_name, '--output', str(self.root / 'build/run/out.wav'))
        self.call('host', 'stop')
        self.assertEqual(process.wait(timeout=5), 0)
        native = next(event for event in self.events() if event[0] == 'native')
        self.assertIn(input_name, native)
        self.assertFalse((self.root / 'injected').exists())

    def test_device_environment_reaches_native_cli(self):
        self.env.update(MOZART_CAPTURE='test capture', MOZART_PLAYBACK='test playback')
        process = self.spawn()
        self.call('host', 'stop')
        self.assertEqual(process.wait(timeout=5), 0)
        native = next(event for event in self.events() if event[0] == 'native')
        self.assertEqual(native[native.index('--capture') + 1], 'test capture')
        self.assertEqual(native[native.index('--playback') + 1], 'test playback')

    def test_build_configuration(self):
        for role in ('host', 'pre'):
            self.call(role, 'build')
        events = self.events()
        self.assertEqual([event[0] for event in events], ['cmake'] * 4)
        self.assertIn('-DCMAKE_BUILD_TYPE=Release', events[0])
        self.assertIn('-DMOZART_USE_RNNOISE=OFF', events[2])
        self.assertIn(str(self.root / 'build/usb-pre'), events[2])

    def test_stop_waits_for_drain_and_restores_active_service_and_sink(self):
        process = self.spawn()
        status = self.call('host', 'status').stdout
        self.assertIn('process=running', status)
        self.assertIn('"state":"ready"', status)
        self.call('host', 'stop')
        self.assertEqual(process.wait(timeout=5), 0)
        events = self.events()
        native_args = next(event for event in events if event[0] == 'native')
        self.assertIn('hw:CARD=sofhdadsp,DEV=0', native_args)
        drain = events.index(['native', 'STOP/drain'])
        resume = events.index(['pactl', 'suspend-sink', 'test-sink', '0'])
        restart = events.index(['systemctl', 'start', 'mozart-usb-audio.service'])
        self.assertLess(drain, resume)
        self.assertLess(drain, restart)
        self.assertFalse((self.run_dir / 'native.pid').exists())

    def test_file_mode_does_not_access_pulse_and_inactive_service_stays_inactive(self):
        self.env['LEGACY_STATE'] = 'inactive'
        process = self.spawn('--input', 'input.wav', '--output', str(self.root / 'build/run/output.wav'))
        self.call('host', 'rollback')
        self.assertEqual(process.wait(timeout=5), 0)
        events = self.events()
        self.assertFalse(any(event[0] == 'pactl' for event in events))
        self.assertFalse(any(event[:2] == ['systemctl', 'start'] for event in events))
        native = next(event for event in events if event[0] == 'native')
        self.assertNotIn('--capture', native)
        self.assertNotIn('--playback', native)

    def test_already_suspended_sink_is_not_resumed(self):
        self.env['SINK_STATE'] = 'SUSPENDED'
        process = self.spawn()
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=20), 143)
        self.assertFalse(any(event[:2] == ['pactl', 'suspend-sink'] for event in self.events()))

    def test_failure_is_not_success_and_recovery_is_retained(self):
        self.env.update(NATIVE_FAIL='7', RESTORE_FAIL='1')
        result = self.call('host', 'run', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.run_dir / 'legacy.restore').exists())
        self.assertIn('restore=pending', self.call('host', 'status').stdout)
        self.env.pop('RESTORE_FAIL')
        self.call('host', 'rollback')
        self.assertFalse((self.run_dir / 'legacy.restore').exists())

    def test_native_failure_exit_code_is_preserved(self):
        self.env['NATIVE_FAIL'] = '7'
        result = self.call('host', 'run', check=False)
        self.assertEqual(result.returncode, 7)
        self.assertIn(['systemctl', 'start', 'mozart-usb-audio.service'], self.events())

    def test_unrecognized_pid_is_not_signalled(self):
        self.run_dir.mkdir(parents=True)
        (self.run_dir / 'native.pid').write_text(str(os.getpid()) + '\n')
        result = self.call('host', 'stop', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('未知进程', result.stderr)
        self.assertEqual(self.events(), [])
        (self.run_dir / 'native.pid').unlink()

    def test_duplicate_run_fails_without_interrupting_owner(self):
        process = self.spawn()
        result = self.call('host', 'run', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(process.poll())
        self.assertEqual(sum(event[:2] == ['systemctl', 'stop'] for event in self.events()), 1)
        self.call('host', 'stop')

    def test_background_start_and_rollback(self):
        self.call('host', 'start')
        self.assertTrue((self.run_dir / 'native.log').exists())
        self.assertIn('process=running', self.call('host', 'status').stdout)
        self.call('host', 'rollback')
        self.assertIn('process=stopped', self.call('host', 'status').stdout)

    def test_output_escape_and_build_symlink_are_rejected(self):
        self.assertNotEqual(self.call('host', 'run', '--output', '/tmp/out.wav', check=False).returncode, 0)
        shutil.rmtree(self.root / 'build/native-host')
        (self.root / 'build/native-host').symlink_to('/usr/bin', target_is_directory=True)
        self.assertNotEqual(self.call('host', 'build', check=False).returncode, 0)
        self.assertEqual(self.events(), [])


if __name__ == '__main__':
    unittest.main()
