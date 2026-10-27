"""实时启动入口的全链路编排测试；模型与声卡使用有限状态进程替代。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]

FAKE_STACK = '''import argparse, json, os, signal, time
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--run-dir', type=Path)
a, rest = p.parse_known_args()
a.run_dir.mkdir(parents=True, exist_ok=True)
(a.run_dir/'argv.json').write_text(json.dumps(rest))
status = a.run_dir/'stack-status.json'
stopped = False
def stop(*_):
    global stopped
    stopped = True
signal.signal(signal.SIGTERM, signal.SIG_IGN if os.environ.get('IGNORE_STACK_TERM') else stop)
if os.environ.get('IGNORE_STACK_TERM'):
    import subprocess, sys
    child = subprocess.Popen([sys.executable, '-c',
        'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])
    (a.run_dir/'child.pid').write_text(str(child.pid))
state = {'supervisor_pid': os.getpid(), 'state': 'waiting', 'phase': 'waiting_for_audio',
         'readiness': {'translation': True}}
if os.environ.get('FAIL_STACK'):
    state.update(state='failed', phase='failed', error={'message': 'translator unavailable'})
def save():
    tmp = status.with_suffix('.tmp')
    tmp.write_text('{' if os.environ.get('FAIL_STACK') == 'bad_json' else json.dumps(
        {} if os.environ.get('FAIL_STACK') == 'missing_fields' else state))
    tmp.replace(status)
save()
while not stopped:
    pre = Path(os.environ['RUN_DIR'])/'pre-status.json'
    if pre.exists() and not os.environ.get('FAIL_STACK'):
        data = json.loads(pre.read_text())
        if data.get('state') == 'ready':
            state.update(state='running', phase='capturing')
            save()
    time.sleep(.02)
if state['state'] != 'failed':
    state.update(state='stopped', phase='stopped')
save()
'''

FAKE_PRE = '''#!/usr/bin/env python3
import json, os, signal, sys, time
from pathlib import Path
args = sys.argv[1:]
path = Path(args[args.index('--status-file')+1])
(path.parent/'pre-argv.json').write_text(json.dumps(args))
stopped = False
def stop(*_):
    global stopped
    stopped = True
signal.signal(signal.SIGTERM, stop)
def save(state):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps({'pid': os.getpid(), 'state': state, 'frames': 100,
                               'reason': 'device unavailable' if state == 'failed' else ''}))
    tmp.replace(path)
save('failed' if os.environ.get('FAIL_PRE') else 'ready')
while not stopped:
    time.sleep(.02)
save('stopped')
'''

FAKE_CURL = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
run = Path(os.environ['RUN_DIR'])
receipt = run/'stack-run-dir'
if not receipt.exists() or not (Path(receipt.read_text().strip())/'stack-status.json').exists():
    sys.exit(7)
url = next(arg for arg in sys.argv if arg.startswith('http:'))
if os.environ.get('FAIL_MODE') and url.endswith('/api/mode/switch'):
    sys.exit(22)
if url.endswith('/api/status'):
    print(json.dumps({'mode': 'rt_rvc', 'model': {'has_index': 'true'}, 'stream': {}}))
else:
    print('{}')
'''


class RealtimeStackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mozart-stack-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root/'run'
        self.captions = self.root/'captions'
        (self.root/'tools').mkdir()
        shutil.copy2(ROOT/'tools/run-realtime-voice.sh', self.root/'tools/run-realtime-voice.sh')
        (self.root/'tools/run_translated_speech.py').write_text(FAKE_STACK)
        self.executable('build/usb-pre/mozart-pre', FAKE_PRE)
        self.executable('build-gpu/state/mozart_stated', '#!/bin/sh\nexit 99\n')
        for name, device in [('arecord', 'MIC'), ('aplay', 'Device')]:
            self.executable('bin/'+name, f'#!/bin/sh\necho plughw:CARD={device},DEV=0\n')
        self.executable('bin/curl', FAKE_CURL)
        self.config = self.root/'backend.yaml'
        self.config.write_text('fixture config')
        self.env = {**os.environ, 'PATH': str(self.root/'bin')+os.pathsep+os.environ['PATH'],
                    'HOME': str(self.root), 'RUN_DIR': str(self.run), 'CAPTIONS_DIR': str(self.captions),
                    'CONFIG': str(self.config), 'PYTHON': sys.executable, 'READY_TIMEOUT': '5', 'STOP_TIMEOUT': '5',
                    'API': 'http://127.0.0.1:18181', 'FINAL_MODEL': '',
                    'GLOSSARY': '/fixture/glossary.json', 'CORRECTIONS': '/fixture/corrections.json'}
        self.addCleanup(self.command, 'stop')

    def executable(self, path, source):
        target = self.root/path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
        target.chmod(0o755)

    def command(self, action):
        return subprocess.run(['bash', str(self.root/'tools/run-realtime-voice.sh'), action],
                              env=self.env, text=True, capture_output=True, timeout=15)

    def assert_not_running(self, pid):
        result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
        self.assertTrue(not result.stdout.strip() or result.stdout.strip().startswith('Z'), result.stdout)

    def recorded_pids(self):
        return [int((self.run/name).read_text()) for name in ('pre.pid', 'stack.pid')
                if (self.run/name).exists()]

    def test_start_uses_one_supervisor_and_dual_send_then_stop_releases_both(self):
        result = self.command('start')
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads((self.captions/'argv.json').read_text())
        for flag in ('--external-capture', '--no-playback', '--archive-utterances'):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index('--mode')+1], 'captions')
        self.assertEqual(argv[argv.index('--api-port')+1], '18181')
        self.assertEqual(argv[argv.index('--glossary')+1], self.env['GLOSSARY'])
        self.assertEqual(argv[argv.index('--corrections')+1], self.env['CORRECTIONS'])
        pre = json.loads((self.run/'pre-argv.json').read_text())
        self.assertEqual(pre[pre.index('-b')+1], '127.0.0.1:18100')
        self.assertEqual(pre[pre.index('-d')+1], 'plughw:CARD=MIC,DEV=0')
        self.assertEqual(pre[pre.index('-o')+1], 'plughw:CARD=Device,DEV=0')
        status = self.command('status')
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn('phase=capturing', status.stdout)
        pids = self.recorded_pids()
        result = self.command('stop')
        self.assertEqual(result.returncode, 0, result.stderr)
        for pid in pids:
            self.assert_not_running(pid)
        self.assertFalse((self.run/'pre.pid').exists())
        self.assertFalse((self.run/'stack.pid').exists())
        self.assertEqual(json.loads((self.captions/'stack-status.json').read_text())['state'], 'stopped')
        self.assertEqual(json.loads((self.run/'pre-status.json').read_text())['state'], 'stopped')
        self.assertTrue((self.run/'pre.pid.stopped').exists())
        self.assertTrue((self.captions/'runtime-final/pre-status.json').exists())
        self.assertTrue((self.captions/'runtime-final/pre.log').exists())

    def test_translation_failure_prevents_capture_and_cleans_up(self):
        self.env['FAIL_STACK'] = '1'
        result = self.command('start')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('translator unavailable', result.stderr)
        self.assertFalse((self.run/'pre-argv.json').exists())
        self.assertFalse((self.run/'stack.pid').exists())
        self.assertEqual(json.loads((self.captions/'stack-status.json').read_text())['state'], 'failed')

    def test_backend_mode_failure_cleans_up_supervisor(self):
        self.env['FAIL_MODE'] = '1'
        result = self.command('start')
        self.assertEqual(result.returncode, 22)
        self.assertIn('后端请求失败: POST /api/mode/switch', result.stderr)
        self.assertFalse((self.run/'stack.pid').exists())
        self.assertFalse((self.run/'pre-argv.json').exists())
        self.assertEqual(json.loads((self.captions/'stack-status.json').read_text())['state'], 'stopped')

    def test_stop_escalates_and_reaps_unresponsive_supervisor_descendants(self):
        self.env['IGNORE_STACK_TERM'] = '1'
        result = self.command('start')
        self.assertEqual(result.returncode, 0, result.stderr)
        pids = self.recorded_pids() + [int((self.captions/'child.pid').read_text())]
        self.env['STOP_TIMEOUT'] = '1'
        result = self.command('stop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('强制回收进程树', result.stderr)
        for pid in pids:
            self.assert_not_running(pid)

    def test_bad_pre_pid_does_not_prevent_stopping_the_stack(self):
        result = self.command('start')
        self.assertEqual(result.returncode, 0, result.stderr)
        pre_pid = int((self.run/'pre.pid').read_text())
        stack_pid = int((self.run/'stack.pid').read_text())
        (self.run/'pre.pid').write_text('0')
        try:
            result = self.command('stop')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('无效 PID', result.stderr)
            self.assert_not_running(stack_pid)
            self.assertFalse((self.run/'stack.pid').exists())
        finally:
            (self.run/'pre.pid').write_text(str(pre_pid))

    def test_invalid_pid_does_not_signal_a_process_group(self):
        self.run.mkdir()
        receipt = self.run/'pre.pid'
        receipt.write_text('0')
        try:
            result = self.command('stop')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('无效 PID', result.stderr)
        finally:
            receipt.unlink()

    def test_preprocessor_failure_cleans_up_supervisor(self):
        self.env['FAIL_PRE'] = '1'
        result = self.command('start')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('device unavailable', result.stderr)
        self.assertFalse((self.run/'pre.pid').exists())
        self.assertFalse((self.run/'stack.pid').exists())
        self.assertEqual(json.loads((self.captions/'stack-status.json').read_text())['state'], 'stopped')

    def test_malformed_stack_status_fails_instead_of_starting_capture(self):
        self.env['FAIL_STACK'] = 'bad_json'
        result = self.command('start')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('字幕状态文件损坏', result.stderr)
        self.assertFalse((self.run/'stack.pid').exists())
        self.assertFalse((self.run/'pre-argv.json').exists())

    def test_missing_stack_status_fields_prevent_capture(self):
        self.env['FAIL_STACK'] = 'missing_fields'
        result = self.command('start')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('字幕状态文件缺少必要字段', result.stderr)
        self.assertFalse((self.run/'stack.pid').exists())
        self.assertFalse((self.run/'pre-argv.json').exists())

    def test_stop_does_not_signal_an_unrelated_process(self):
        process = subprocess.Popen(['sleep', '30'])
        self.run.mkdir()
        receipt = self.run/'pre.pid'
        receipt.write_text(str(process.pid))
        try:
            result = self.command('stop')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('PID 所属进程', result.stderr)
            self.assertIsNone(process.poll())
        finally:
            receipt.unlink(missing_ok=True)
            process.terminate()
            process.wait(timeout=3)

    def test_previous_runtime_log_is_preserved(self):
        self.run.mkdir()
        (self.run/'pre.log').write_text('historical input diagnosis')
        result = self.command('start')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.captions/'previous-runtime/pre.log').read_text(), 'historical input diagnosis')


if __name__ == '__main__':
    unittest.main()
