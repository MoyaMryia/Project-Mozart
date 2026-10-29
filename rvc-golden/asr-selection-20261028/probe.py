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

def semantic_contract():
    """加载离线语义契约。路径由 translation_semantics 定义一次。"""
    tools = Path(__file__).resolve().parents[2] / 'tools'
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    import translation_semantics
    return translation_semantics.load_contract()


def regression_checks(rows, contract=None, reserved_results=None):
    expected_terms = {8: 'genshin impact', 16: 'chinese and english', 17: 'chinese and english',
                      39: 'i am stunned brother', 41: 'powerless',
                      42: 'united kingdom', 43: 'united kingdom'}
    by_seq = {row['seq']: row for row in rows}
    checks = {'all_candidates_completed': all(row['candidate']['status'] == 'completed'
                                             and row['candidate'].get('en', '').strip() for row in rows)}
    for seq, term in expected_terms.items():
        row = by_seq.get(seq)
        checks['record_' + str(seq)] = row is not None and term in row['candidate'].get('en', '').casefold()
    contract = contract or semantic_contract()
    checks.update(contract.checks({seq: row['candidate'].get('en', '') for seq, row in by_seq.items()}))
    if reserved_results is not None:
        from translation_semantics import passes
        for case in contract.cases:
            if case.seq is None:
                result = reserved_results.get(case.id)
                checks['semantic_' + case.id] = (result is not None and result['status'] == 'completed'
                                                 and passes(case, result['en']))
    return checks


def validate_recorded_candidate(candidate):
    if not isinstance(candidate, dict) or candidate.get('status') not in ('completed', 'failed'):
        raise ValueError('Recorded candidates need a completed or failed status')
    if not isinstance(candidate.get('en', ''), str):
        raise ValueError('Recorded candidate text must be a string')


def recorded_results(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('records'), list) or not data['records']:
        raise ValueError('Recorded results need a nonempty records array')
    seen = set()
    for row in data['records']:
        if not isinstance(row, dict) or isinstance(row.get('seq'), bool) or not isinstance(row.get('seq'), int):
            raise ValueError('Each recorded result needs an integer sequence')
        if row['seq'] in seen:
            raise ValueError('Recorded result sequences must be unique')
        seen.add(row['seq'])
        validate_recorded_candidate(row.get('candidate'))
    reserved = data.get('reserved_results')
    if reserved is not None:
        if not isinstance(reserved, dict):
            raise ValueError('Reserved results must be an object')
        for candidate in reserved.values():
            validate_recorded_candidate(candidate)
    return data


def main():
    home = Path.home()
    parser = argparse.ArgumentParser()
    parser.add_argument('--recorded-results', type=Path, help='Check saved outputs without starting a model')
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--work-dir', type=Path, default=Path('/tmp/mozart-asr-offline'))
    parser.add_argument('--baseline-tools', type=Path, default=Path('/tmp/mozart-asr-baseline/tools'))
    parser.add_argument('--llama-server', type=Path,
        default=home / 'mozart-archive/qwen35/llama.cpp/build-cuda-bin/llama-server')
    parser.add_argument('--llama-model', type=Path,
        default=home / 'mozart-archive/qwen35/gguf/qwen35-0.8b-text-Q4_K_M.gguf')
    args = parser.parse_args()
    if args.recorded_results is not None:
        try:
            data = recorded_results(args.recorded_results)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        checks = regression_checks(data['records'], reserved_results=data.get('reserved_results') or {})
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        raise SystemExit(0 if all(checks.values()) else 1)
    repo, root = args.repo.resolve(), args.work_dir.resolve()
    sys.path.insert(0, str(repo / 'tools'))
    os.environ['MOZART_GLOSSARY'] = str(repo / 'state/glossary.json')
    os.environ['MOZART_CORRECTIONS'] = str(repo / 'state/corrections.json')
    contract = semantic_contract()
    import translation_semantics
    checker_sha256 = hashlib.sha256(Path(translation_semantics.__file__).read_bytes()).hexdigest()
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
            reserved_results = {}
            for case in contract.cases:
                if case.seq is not None:
                    continue
                audit = []
                try:
                    english, _ = new.translate_recognized_event(url, {'text': case.source_zh}, audit=audit)
                except (OSError, ValueError) as error:
                    reserved_results[case.id] = {'status': 'failed', 'error': str(error), 'audit': audit}
                else:
                    reserved_results[case.id] = {'status': 'completed', 'en': english, 'audit': audit}
            checks = regression_checks(results, contract, reserved_results)
            inputs = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                      for name in ('subtitle_bridge.py', 'asr_checks.py', 'records.json')}
            inputs['translation-semantics.json'] = contract.source_sha256
            inputs['translation_semantics.py'] = checker_sha256
            (root / 'results.json').write_text(json.dumps(
                {'command': command, 'input_sha256': inputs, 'records': results,
                 'reserved_results': reserved_results, 'checks': checks},
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
