#!/usr/bin/env python3
"""同一故障用例必须拒绝旧实现，并通过当前实现。"""
import argparse
import ast
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'tools'), str(ROOT/'tools/tests')]
import speech_service
from test_speech_runtime import SpeechRuntimeTests


def suite():
    return unittest.TestSuite([SpeechRuntimeTests(
        'test_fatal_error_fails_job_already_dequeued_for_admission')])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='f7e48fe')
    args = parser.parse_args()
    source = subprocess.check_output(
        ['git', 'show', f'{args.base}:tools/speech_service.py'], cwd=ROOT, text=True)
    cls = next(node for node in ast.parse(source).body
               if isinstance(node, ast.ClassDef) and node.name == 'SpeechService')
    method = next(node for node in cls.body
                  if isinstance(node, ast.FunctionDef) and node.name == 'fatal_error')
    namespace = dict(vars(speech_service))
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<baseline-fatal-error>', 'exec'), namespace)
    with patch.object(speech_service.SpeechService, 'fatal_error', namespace['fatal_error']):
        old = unittest.TextTestRunner(verbosity=2).run(suite())
    assert len(old.failures) == 1 and not old.errors, old
    assert 'job did not finish' in old.failures[0][1], old.failures
    current = unittest.TextTestRunner(verbosity=2).run(suite())
    assert current.wasSuccessful(), current
    print('旧实现的受理等待项无法结束；当前实现将该任务标为失败。')


if __name__ == '__main__':
    main()
