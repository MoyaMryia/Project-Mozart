"""Compare the whole-utterance control and the boundary classifier replay."""
import argparse
from collections import Counter
import json
from pathlib import Path

from analyze_clauses import analyze, stats


def classifier_results(root, result):
    seen, decisions, errors = set(), [], []
    for line in (root/'runtime/subtitles.jsonl').read_text().splitlines():
        row = json.loads(line)
        value = row.get('boundary_classifier')
        if value is None:
            continue
        identity = (row['seq'], json.dumps(value, sort_keys=True))
        if identity in seen:
            continue
        seen.add(identity)
        if value.get('error'):
            errors.append({'seq': row['seq'], **value})
        else:
            decisions.append(value)
    parts = [part for parent in result['parents'] for part in parent['segments'] if part.get('early')]
    result['classifier'] = {
        'accepted_decisions': len(decisions),
        'accepted_decision_ms': stats([item['elapsed_ms'] for item in decisions]),
        'errors': errors,
        'early_parent_count': sum(any(p.get('early') for p in parent['segments']) for parent in result['parents']),
        'early_reasons': dict(Counter(part['boundary_classifier']['cut']['reason'] for part in parts)),
        'source_corrections': sum(part.get('source_correction', False) for parent in result['parents'] for part in parent['segments']),
        'note': 'Timings include accepted classifier results only. Discarded stale results are not in the caption log.'}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('control', type=Path)
    parser.add_argument('experiment', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    control = analyze(args.control)
    experiment = classifier_results(args.experiment, analyze(args.experiment))
    right = {row['seq']: row for row in experiment['parents']}
    deltas, changed = [], []
    for row in control['parents']:
        other = right.get(row['seq'])
        if other is None or other['source'] != row['source']:
            changed.append(row['seq'])
        elif other['first_play_minus_final_emit_seconds'] is not None and row['first_play_minus_final_emit_seconds'] is not None:
            deltas.append(other['first_play_minus_final_emit_seconds']-row['first_play_minus_final_emit_seconds'])
    result = {'control': control, 'experiment': experiment, 'changed_source_sequences': changed,
              'matched_source_delay_deltas_seconds': stats(deltas)}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({
        'control': {k: control[k] for k in ('parent_count', 'completed_speech_parents', 'first_play_minus_final_emit_seconds', 'output_duration_seconds')},
        'experiment': {k: experiment[k] for k in ('parent_count', 'completed_speech_parents', 'first_play_minus_final_emit_seconds', 'output_duration_seconds', 'classifier')},
        'changed_source_sequences': changed, 'matched_delay_deltas_seconds': stats(deltas)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
