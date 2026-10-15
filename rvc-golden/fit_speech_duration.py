#!/usr/bin/env python3
"""标定四参数时长模型。按父字幕留出，另测独立文本。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import wave

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from speech_chunks import estimated_audio_seconds
from speech_duration import duration_features, DurationEstimator, FEATURE_NAMES, FEATURE_VERSION


def metrics(prediction, actual):
    error = np.array(prediction)-np.array(actual)
    return dict(count=len(error), mae_seconds=float(np.mean(abs(error))),
        p90_absolute_seconds=float(np.percentile(abs(error), 90)),
        bias_seconds=float(np.mean(error)), maximum_underestimate_seconds=float(max(0, -min(error))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replay', type=Path, required=True)
    parser.add_argument('--independent', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    jobs_path = args.replay/'jobs.json'
    jobs = json.loads(jobs_path.read_text())
    rows = []
    for job in jobs:
        if job['status'] != 'completed' or job['language'] != 'en':
            raise ValueError('Calibration requires complete English jobs')
        if len(job['chunk_texts']) != len(job['text_part_results']):
            raise ValueError('Calibration parts do not match')
        for index, (text, result) in enumerate(zip(job['chunk_texts'], job['text_part_results'])):
            path = args.replay/'speech/results'/f"{job['id']}-part-{index:03d}.wav"
            with wave.open(str(path), 'rb') as audio:
                duration = audio.getnframes()/audio.getframerate()
                if abs(duration-result['duration_seconds']) > 1/audio.getframerate():
                    raise ValueError('Calibration duration differs from WAV')
            rows.append(dict(parent=job['utterance_id'].split('/part/')[0], job=job['id'],
                text=text, seconds=duration, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    x = np.array([duration_features(r['text']) for r in rows])
    y = np.array([r['seconds'] for r in rows])
    penalty = np.eye(len(FEATURE_NAMES))*10.
    penalty[0, 0] = 0
    def fit(mask):
        return np.linalg.solve(x[mask].T@x[mask]+penalty, x[mask].T@y[mask])
    prediction = np.zeros(len(rows))
    parents = sorted({r['parent'] for r in rows})
    for parent in parents:
        test = np.array([r['parent'] == parent for r in rows])
        prediction[test] = np.maximum(.4, x[test]@fit(~test))
    baseline = np.array([estimated_audio_seconds(r['text']) for r in rows])
    coefficients = fit(np.ones(len(rows), dtype=bool))
    if not np.isfinite(coefficients).all() or min(coefficients) < 0:
        raise ValueError('Duration model requires finite nonnegative coefficients')
    independent = json.loads(args.independent.read_text())
    runtime = independent['runtime']
    scope = {k: runtime[k] for k in ('engine', 'provider_requested', 'precision', 'runtime',
        'num_steps', 'seed', 'audio_stream_protocol', 'model_sha256')}
    revisions = {j['voice_revision'] for j in jobs}
    if len(revisions) != 1:
        raise ValueError('Calibration requires one reference revision')
    profile = dict(id='qiqi-pocket-int8-v1', feature_version=FEATURE_VERSION, features=FEATURE_NAMES,
        coefficients=coefficients.tolist(), voice_revision=revisions.pop(), runtime=scope,
        min_words=float(min(x[:, 1])), max_words=float(max(x[:, 1])),
        reserve_margin_seconds=float(np.ceil(max(0, np.percentile(y-prediction, 90))*20)/20),
        training=dict(parts=len(rows), parents=len(parents), jobs=len(jobs), ridge_alpha=10,
            jobs_sha256=hashlib.sha256(jobs_path.read_bytes()).hexdigest(), validation='leave_one_parent_out'))
    args.profile.parent.mkdir(parents=True, exist_ok=True)
    args.profile.write_text(json.dumps(profile, ensure_ascii=False, indent=2)+'\n')
    estimator = DurationEstimator(args.profile)
    holdout = []
    for row in independent['records']:
        text = row['request']['text']
        result = row['result']
        if 'error' in result or not row['audio']['finite']:
            raise ValueError('Independent synthesis failed')
        if any(text == item['text'] for item in rows):
            raise ValueError('Independent text overlaps calibration text')
        estimate = estimator.predict([text], 'en', profile['voice_revision'], runtime)
        actual = row['audio']['samples']/row['audio']['sample_rate']
        holdout.append(dict(text=text, actual_seconds=actual, **estimate,
            baseline_seconds=estimated_audio_seconds(text)))
    job_keys = sorted({r['job'] for r in rows})
    def grouped(values):
        return [sum(v for r, v in zip(rows, values) if r['job'] == key) for key in job_keys]
    report = dict(profile=profile, calibration_rows=rows,
        exploratory_parent_holdout=dict(parts=metrics(prediction, y), baseline_parts=metrics(baseline, y),
            jobs=metrics(grouped(prediction), grouped(y)), baseline_jobs=metrics(grouped(baseline), grouped(y))),
        independent=dict(rows=holdout, predictor=metrics([r['seconds'] for r in holdout], [r['actual_seconds'] for r in holdout]),
            baseline=metrics([r['baseline_seconds'] for r in holdout], [r['actual_seconds'] for r in holdout])),
        independent_sha256=hashlib.sha256(args.independent.read_bytes()).hexdigest())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('calibration_rows', 'profile', 'independent')}, indent=2))
    print(json.dumps(report['independent']['predictor'], indent=2))


if __name__ == '__main__':
    main()
