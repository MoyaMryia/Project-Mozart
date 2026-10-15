#!/usr/bin/env python3
"""复用保存的原文，比较漏译重试并保留全部候选。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--replay', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.repo/'tools'))
    import subtitle_bridge as bridge
    args.output.mkdir(parents=True, exist_ok=False)
    latest = {}
    for line in (args.replay/'runtime/subtitles.jsonl').read_text().splitlines():
        row = json.loads(line)
        latest[row['seq']] = row
    sources = []
    for row in latest.values():
        context = []
        for part in row.get('translation_segments', []):
            sources.append(dict(seq=row['seq'], source=part['source_text'], context=list(context[-2:]),
                previous=part.get('en'), previous_status=part['translation_status']))
            context.append(dict(source=part['source_text'], translation=part.get('en','')))
    binary = Path.home()/'mozart-archive/qwen35/llama.cpp/build-cuda-bin/llama-server'
    command = [str(binary), '-m', str(Path.home()/'mozart-archive/qwen35/gguf/qwen35-0.8b-text-Q4_K_M.gguf'),
        '-c','2048','-b','256','-ub','128','--cache-ram','128','-ngl','99','-np','1','-t','2','-tb','2',
        '--host','127.0.0.1','--port','18220','--jinja']
    environment = dict(os.environ, LD_LIBRARY_PATH=str(binary.parent)+':'+os.environ.get('LD_LIBRARY_PATH',''))
    url = 'http://127.0.0.1:18220'
    with (args.output/'server.log').open('w') as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=environment)
        try:
            deadline = time.monotonic()+90
            while True:
                try:
                    with urllib.request.urlopen(url+'/health', timeout=1):
                        break
                except OSError:
                    if process.poll() is not None or time.monotonic()>deadline:
                        raise RuntimeError('隔离翻译服务未就绪')
                    time.sleep(.2)
            records = []
            for case in ('baseline', 'candidate'):
                for source in sources:
                    audit = []
                    start = time.monotonic()
                    row = dict(source, case=case)
                    try:
                        row['translation'], _ = bridge.translate(url, source['source'], context=source['context'], audit=audit, repair_untranslated=case=='candidate')
                        row['status'] = 'completed'
                    except Exception as error:
                        row.update(status='failed', error=str(error))
                    row.update(audit=audit, elapsed_seconds=time.monotonic()-start)
                    records.append(row)
                    (args.output/'results.json').write_text(json.dumps(records, ensure_ascii=False, indent=2)+'\n')
                    print(json.dumps(row, ensure_ascii=False), flush=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    main()
