#!/usr/bin/env python3
"""固定提交时间、播放顺序和音频时长，计算零计算耗时的播放下界。"""
import argparse
import json
from pathlib import Path
import statistics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    jobs = sorted(json.loads((args.root/'jobs.json').read_text()), key=lambda j: j['created_at'])
    parents = {}
    for row in (args.root/'runtime/subtitles.jsonl').read_text().splitlines():
        item = json.loads(row)
        parents[item['seq']] = item
    results = []
    for speed in [1.0, 1.05, 1.1, 1.15, 1.3, 1.4, 1.5]:
        available = 0
        first, records = {}, []
        for job in jobs:
            start = max(job['created_at'], available)
            duration = job['duration_seconds']/speed
            available = start+duration
            seq = int(job['utterance_id'].rsplit(':',1)[-1].split('/part/')[0])
            first.setdefault(seq, start)
            records.append(dict(job=job['id'], seq=seq, ideal_start=start,
                actual_start=job['playback_started_at'], duration_seconds=duration))
        delays = [(start-parents[seq]['asr_emitted_at'])*1000 for seq,start in first.items()]
        results.append(dict(speed=speed, parent_count=len(delays), job_count=len(jobs),
            parent_final_to_first_play_median_ms=statistics.median(delays),
            parent_final_to_first_play_max_ms=max(delays),
            output_duration_seconds=sum(j['duration_seconds']/speed for j in jobs), records=records))
    args.output.write_text(json.dumps(dict(
        method='推导；固定任务提交、FIFO 顺序和完整文本；假设生成、切换、落盘耗时均为零。',
        limitations='speed > 1 仅为时长缩放推导，未验证保音高变速的音质。不是设备实测。',
        results=results),ensure_ascii=False,indent=2)+'\n')


if __name__ == '__main__':
    main()
