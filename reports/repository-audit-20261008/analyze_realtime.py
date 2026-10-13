"""Inspect saved speech timings. Do not run inference or change source files."""
import hashlib
import json
from pathlib import Path
import statistics


def stats(values):
    return {'count': len(values), 'median': statistics.median(values),
            'min': min(values), 'max': max(values), 'sum': sum(values)}


def overlap(start, end, intervals):
    return sum(max(0, min(end, right)-max(start, left)) for left, right in intervals)


def analyze(root):
    base = root/'rvc-golden/sentence-context-20261007/short-v2'
    jobs_path = base/'jobs.json'
    captions_path = base/'runtime/subtitles.jsonl'
    jobs = sorted(json.loads(jobs_path.read_text()), key=lambda job: job['created_at'])
    captions = [json.loads(line) for line in captions_path.read_text().splitlines() if line.strip()]
    by_id = {caption['utterance_id']: caption for caption in captions}
    assert all(job['status'] == 'completed' for job in jobs)
    chunks = sorted([chunk for job in jobs for chunk in job['chunks']],
                    key=lambda chunk: chunk['playback_started_at'])
    intervals = [(chunk['playback_started_at'], chunk['finished_at']) for chunk in chunks]
    assert all(left[1] <= right[0] for left, right in zip(intervals, intervals[1:]))
    rows = []
    ideal_end = 0
    for job in jobs:
        ready, play = job['audio_ready_at'], job['playback_started_at']
        busy = overlap(ready, play, intervals)
        previous_slots = [(other['started_at'], other['generation_finished_at'])
                          for other in jobs if other['started_at'] < job['started_at']]
        occupied = overlap(job['created_at'], job['started_at'], previous_slots)
        ideal_start = max(job['created_at'], ideal_end)
        ideal_end = ideal_start+job['duration_seconds']
        emitted = by_id[job['utterance_id']]['asr_emitted_at']
        rows.append({
            'seq': int(job['utterance_id'].rsplit(':', 1)[-1]),
            'queue_seconds': job['started_at']-job['created_at'],
            'queue_outside_previous_job_slots_seconds': max(0, job['queue_seconds']-occupied),
            'ready_wait_seconds': play-ready,
            'ready_wait_playing_seconds': busy,
            'ready_wait_idle_seconds': max(0, play-ready-busy),
            'first_piece_synthesis_seconds': job['chunks'][0]['synthesis_seconds'],
            'first_piece_audio_seconds': job['chunks'][0]['duration_seconds'],
            'first_piece_words': len(job['chunks'][0]['text'].split()),
            'asr_to_play_seconds': play-emitted,
            'instant_synthesis_asr_to_play_seconds': ideal_start-emitted,
        })
    demand = {}
    for window in (10, 20, 30):
        demand[str(window)] = max(sum(job['duration_seconds'] for job in jobs
            if first['created_at'] <= job['created_at'] < first['created_at']+window)
            for first in jobs)
    fields = [key for key in rows[0] if key != 'seq']
    return {
        'analysis_only': True,
        'source_sha256': {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in (jobs_path, captions_path)},
        'source_captions': len(captions), 'completed_jobs': len(jobs), 'chunks': len(chunks),
        'metrics': {field: stats([row[field] for row in rows]) for field in fields},
        'instant_synthesis_assumptions': [
            'Keep the same 18 jobs and their recorded submission times.',
            'Keep every recorded audio duration and FIFO playback order.',
            'Use normal playback speed and no gaps between available jobs.',
            'Make all audio available at submission; remove generation and admission waits.',
            'Exclude the six withheld captions and physical audio latency.',
        ],
        'maximum_audio_seconds_submitted_in_window': demand,
        'rows': rows,
    }


if __name__ == '__main__':
    print(json.dumps(analyze(Path(__file__).resolve().parents[2]), indent=2))
