import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import urllib.error

def regression_checks(rows):
    expected_terms = {8: 'genshin impact', 16: 'chinese and english', 17: 'chinese and english',
                      39: 'i am stunned brother', 41: 'powerless',
                      42: 'united kingdom', 43: 'united kingdom'}
    by_seq = {row['seq']: row for row in rows}
    checks = {'all_candidates_completed': all(row['candidate']['status'] == 'completed' for row in rows)}
    for seq, term in expected_terms.items():
        checks['record_' + str(seq)] = term in by_seq[seq]['candidate'].get('en', '').casefold()
    return checks


def main():
    home = Path.home()
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--work-dir', type=Path, default=Path('/tmp/mozart-asr-offline'))
    parser.add_argument('--baseline-tools', type=Path, default=Path('/tmp/mozart-asr-baseline/tools'))
    parser.add_argument('--llama-server', type=Path,
        default=home / 'mozart-archive/qwen35/llama.cpp/build-cuda-bin/llama-server')
    parser.add_argument('--llama-model', type=Path,
        default=home / 'mozart-archive/qwen35/gguf/qwen35-0.8b-text-Q4_K_M.gguf')
    args = parser.parse_args()
    repo, root = args.repo.resolve(), args.work_dir.resolve()
    sys.path.insert(0, str(repo / 'tools'))
    os.environ['MOZART_GLOSSARY'] = str(repo / 'state/glossary.json')
    os.environ['MOZART_CORRECTIONS'] = str(repo / 'state/corrections.json')
    sys.path.insert(0, str(root))

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    base = load('baseline_bridge', args.baseline_tools / 'subtitle_bridge.py')
    new = load('candidate_bridge', root / 'subtitle_bridge.py')
    records = json.loads((root / 'records.json').read_text())['records']
    url = 'http://127.0.0.1:19200'
    command = [str(args.llama_server), '-m', str(args.llama_model),
               '-c', '2048', '-b', '256', '-ub', '128', '-ngl', '99', '-np', '1',
               '-t', '2', '-tb', '2', '--host', '127.0.0.1', '--port', '19200', '--jinja']
    results = []
    environment = dict(os.environ)
    environment['LD_LIBRARY_PATH'] = str(Path(command[0]).parent) + ':' + environment.get('LD_LIBRARY_PATH', '')
    with (root / 'model.log').open('w') as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=environment)
        try:
            deadline = time.monotonic() + 90
            while True:
                if process.poll() is not None:
                    raise RuntimeError('Offline translator exited: ' + str(process.returncode))
                try:
                    with urllib.request.urlopen(url + '/health', timeout=2) as response:
                        ready = json.load(response).get('status') == 'ok'
                except (urllib.error.URLError, TimeoutError):
                    ready = False
                if ready:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('Offline translator startup deadline exceeded')
                time.sleep(.25)
            for record in records:
                refined = new.collapse_terminal_run(new.trim_boundary_echo(record['zh'], record['refined_zh'] or ''))
                refined, _ = new.homophones.correct(refined, new.correction_glossary(), corrections=new.correction_table())
                decision = {}
                selected = new.translation_source(record['zh'], refined, audit=decision)
                result = {'seq': record['seq'], 'raw': record['zh'], 'refined': refined,
                          'recorded_en': record['en'], 'selection': decision}
                paths = [('baseline', base, record['translation_source_text']), ('candidate', new, selected)]
                if record['seq'] % 2:
                    paths.reverse()
                for name, module, text in paths:
                    audit = []
                    began = time.monotonic()
                    event = {'text': text}
                    if name == 'candidate':
                        event['translation_source_audit'] = decision
                    try:
                        english, _ = module.translate_recognized_event(url, event, audit=audit)
                    except (OSError, ValueError) as error:
                        result[name] = {'source': text, 'status': 'failed', 'error': str(error), 'audit': audit}
                    else:
                        result[name] = {'source': text, 'status': 'completed', 'en': english, 'audit': audit}
                    result[name]['elapsed_ms'] = round((time.monotonic() - began) * 1000)
                results.append(result)
                print(json.dumps({k: result[k] for k in ('seq', 'raw', 'baseline', 'candidate')}, ensure_ascii=False), flush=True)
            checks = regression_checks(results)
            inputs = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                      for name in ('subtitle_bridge.py', 'asr_checks.py', 'records.json')}
            (root / 'results.json').write_text(json.dumps(
                {'command': command, 'input_sha256': inputs, 'records': results, 'checks': checks},
                ensure_ascii=False, indent=2) + '\n')
            if not all(checks.values()):
                raise RuntimeError('Offline regression failed: ' + ', '.join(name for name, passed in checks.items() if not passed))
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    print('Offline translator stopped', flush=True)


if __name__ == '__main__':
    main()
