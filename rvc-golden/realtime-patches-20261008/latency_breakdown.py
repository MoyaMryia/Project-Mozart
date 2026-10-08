"""Calculate a lower bound for FIFO playback with fixed jobs and zero synthesis time."""
import argparse
import json
from pathlib import Path
import statistics


def statistics_for(values):
    return {'count': len(values), 'mean': statistics.mean(values),
            'median': statistics.median(values), 'max': max(values)}


def analyze(root):
    jobs = json.loads((root/'jobs.json').read_text())
    completed = sorted([job for job in jobs if job['status'] == 'completed'],
                       key=lambda job: job['created_at'])
    per_job = []
    end = 0
    for job in completed:
        stages = {'queue_seconds': job['started_at']-job['created_at'],
                  'start_to_first_ready_seconds': job['audio_ready_at']-job['started_at'],
                  'first_ready_to_play_seconds': job['playback_started_at']-job['audio_ready_at']}
        actual = job['playback_started_at']-job['created_at']
        assert abs(sum(stages.values())-actual) < 1e-6
        assert min(stages.values()) >= 0
        ideal = max(job['created_at'], end)
        end = ideal+job['duration_seconds']
        assert ideal <= job['playback_started_at']+1e-6
        per_job.append({'seq': int(job['utterance_id'].split(':')[-1]), **stages,
                        'submit_to_play_seconds': actual,
                        'first_piece_compute_seconds': job['chunks'][0]['synthesis_seconds'],
                        'zero_compute_fifo_wait_seconds': ideal-job['created_at'],
                        'zero_compute_reduction_seconds': job['playback_started_at']-ideal})
    return {'accepted_jobs': len(jobs), 'completed_jobs': len(completed), 'per_job': per_job,
            'summary': {key: statistics_for([job[key] for job in per_job])
                        for key in per_job[0] if key != 'seq'} if per_job else {}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('runs', nargs='+', type=Path)
    args = parser.parse_args()
    print(json.dumps({'model_note': 'Zero synthesis time. Fixed completed jobs, submission times, audio durations, and FIFO playback. '
                                   'This lower bound is not a GPU measurement.',
                      'runs': {root.name: analyze(root) for root in args.runs}}, ensure_ascii=False, indent=2))
