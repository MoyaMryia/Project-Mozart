"""Summarize fixed-piece latency and ONNX Runtime provider records."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics


def summary(values):
    return {'count': len(values), 'median': statistics.median(values),
            'mean': statistics.mean(values), 'min': min(values), 'max': max(values)} if values else {'count': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = {'variants': {}, 'profile': {}}
    for label in ['cpu-int8', 'cpu-float', 'cpu-float-new-runtime', 'cuda-float']:
        result = json.loads((args.root/label/'results.json').read_text())
        records = result['records']
        complete = [r for r in records if 'error' not in r['result']]
        warm = [r for r in complete if r['repeat'] == 1]
        report['variants'][label] = {
            'runtime': result.get('runtime'), 'startup_seconds': result.get('startup_seconds'),
            'complete': len(complete), 'submitted': len(records), 'exit_code': result.get('exit_code'),
            'errors': [r for r in records if 'error' in r['result']],
            'warm_synthesis_seconds': summary([r['result']['synthesis_seconds'] for r in warm]),
            'warm_callback_seconds': summary([r['result']['first_callback_seconds'] for r in warm
                                               if r['result']['first_callback_seconds'] is not None]),
            'warm_roundtrip_seconds': summary([r['roundtrip_seconds'] for r in warm]),
            'warm_duration_seconds': summary([r['result']['duration_seconds'] for r in warm]),
            'warm_rtf': summary([r['result']['synthesis_seconds']/r['result']['duration_seconds'] for r in warm]),
            'all_audio_finite': all(r['audio']['finite'] for r in complete) if complete else None,
            'max_audio_peak': max((r['audio']['peak'] for r in complete), default=None)}
    providers, operations = Counter(), defaultdict(Counter)
    files = sorted((args.root/'cuda-profile').glob('ort*.json'))
    for path in files:
        for event in json.loads(path.read_text()):
            fields = event.get('args', {})
            provider = fields.get('provider')
            if provider:
                providers[provider] += 1
                operations[provider][fields.get('op_name', 'unknown')] += 1
    report['profile'] = {'files': [str(p) for p in files], 'node_events': dict(providers),
                         'operations': {p: dict(ops) for p, ops in operations.items()},
                         'scope': 'Profile files can collide because five sessions use the same prefix.'}
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
