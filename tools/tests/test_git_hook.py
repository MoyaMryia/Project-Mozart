"""提交检查保留暂存区，并隔离测试子进程的 Git 环境。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]


class GitHookTests(unittest.TestCase):
    def test_real_hook_checks_index_and_isolates_nested_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'main'
            root.mkdir()
            env = {'PATH': os.environ['PATH'], 'HOME': directory, 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_AUTHOR_NAME': 'Test', 'GIT_COMMITTER_NAME': 'Test',
                   'GIT_AUTHOR_EMAIL': 'test@example.com', 'GIT_COMMITTER_EMAIL': 'test@example.com'}
            def git(*args):
                return subprocess.run(['git', *args], cwd=root, env=env, check=True,
                                      capture_output=True, text=True, timeout=10)
            git('init', '-q')
            git('commit', '--allow-empty', '-qm', 'base')
            git('worktree', 'add', '-qb', 'checked', str(Path(directory)/'linked'))
            root = Path(directory)/'linked'
            env['PATH'] = str(root/'bin') + os.pathsep + os.environ['PATH']
            git('config', 'core.hooksPath', '.githooks')
            for name in ('.githooks', 'tools', 'frontend', 'bin'):
                (root/name).mkdir()
            shutil.copy2(REPO/'.githooks/pre-commit', root/'.githooks/pre-commit')
            shutil.copy2(REPO/'tools/ci-local.sh', root/'tools/ci-local.sh')
            (root/'tools/writing_lint.py').write_text(
                'import json, os, pathlib\n'
                'pathlib.Path("lint-env.json").write_text(json.dumps(dict(os.environ)))\n')
            npm = root/'bin/npm'
            npm.write_text('''#!/usr/bin/env bash
python3 - <<'PY'
import json, os, pathlib, subprocess
root = pathlib.Path.cwd()
nested = root/'dependency'
subprocess.run(['git', 'init', '-q', str(nested)], check=True)
result = {'git_dir': os.environ.get('GIT_DIR'),
          'git_index': os.environ.get('GIT_INDEX_FILE'),
          'nested_root': subprocess.check_output(['git', '-C', str(nested), 'rev-parse', '--show-toplevel'], text=True).strip()}
(root/'result.json').write_text(json.dumps(result))
PY
''')
            npm.chmod(0o755)
            (root/'frontend/input.txt').write_text('staged content')
            git('add', 'frontend/input.txt')
            git('commit', '-qm', 'fixture')
            result = json.loads((root/'result.json').read_text())
            lint_environment = json.loads((root/'lint-env.json').read_text())
            self.assertTrue(lint_environment['GIT_INDEX_FILE'])
            self.assertTrue(lint_environment['GIT_DIR'])
            self.assertIsNone(result['git_dir'])
            self.assertIsNone(result['git_index'])
            self.assertEqual(result['nested_root'], str(root/'dependency'))
            self.assertEqual(git('show', 'HEAD:frontend/input.txt').stdout, 'staged content')
            (root/'frontend/input.txt').write_text('staged update')
            git('add', 'frontend/input.txt')
            (root/'frontend/input.txt').write_text('unstaged update')
            result = subprocess.run(['git', 'commit', '-qm', 'must fail'], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('frontend/input.txt', result.stderr)
            self.assertEqual(git('show', 'HEAD:frontend/input.txt').stdout, 'staged content')


if __name__ == '__main__':
    unittest.main()
