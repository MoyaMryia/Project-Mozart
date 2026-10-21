#!/usr/bin/env bash
# 真实人声验收：用视频里的一段真人说话作为输入，跑识别与翻译，
# 与人工字幕（SRT）比较字符级相似度，并统计翻译结果。
#
# 用法：usb-audio/verify-human-voice.sh <秒> <时长>
# 例：  usb-audio/verify-human-voice.sh 63 60
set -euo pipefail

start="${1:-63}"
seconds="${2:-60}"
root="${MOZART_ROOT:-$HOME/mozart-demo-20261020}"
media="${MOZART_MEDIA:-$HOME/mozart-full-soak-input.mp4}"
srt="${MOZART_SRT:-$root/test-audio_原文.srt}"
run="${MOZART_RUN:-/tmp/human-voice-check}"

cd "$root"
rm -rf "$run"
python3 -u tools/run_translated_speech.py \
  --backend-config state/translated-speech.yaml --mode full \
  --reference "$HOME/Mozart/rvc-golden/clone-tts/qiqi-20261004/reference-qiqi-natural-source.wav" \
  --final-model "$HOME/models/sherpa-onnx/clone-test-downloads/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17" \
  --input "$media" --start "$start" --seconds "$seconds" --no-playback \
  --run-dir "$run" --data-dir "$run/data" > "$run.log" 2>&1

python3 - "$run/subtitles.jsonl" "$srt" "$start" "$seconds" <<'PY'
import difflib, json, sys, pathlib

def srt_text(path, start, seconds):
    """取出时间区间内的人工字幕文本。"""
    blocks, current = [], None
    for line in pathlib.Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if '-->' in line:
            begin = line.split('-->')[0].strip()
            h, m, s = begin.split(':')
            current = int(h)*3600 + int(m)*60 + float(s.replace(',', '.'))
        elif line and current is not None and not line.isdigit():
            if start <= current < start + seconds:
                blocks.append(line)
    return ''.join(blocks)

rows = {}
for line in open(sys.argv[1]):
    record = json.loads(line)
    rows[record['utterance_id']] = record
finals = [r for r in rows.values() if r.get('final')]
spoken = ''.join(r.get('zh', '') for r in finals)
reference = srt_text(sys.argv[2], float(sys.argv[3]), float(sys.argv[4]))
ratio = difflib.SequenceMatcher(None, reference, spoken).ratio()
done = sum(1 for r in finals if r.get('translation_status') == 'completed')
failed = sum(1 for r in finals if r.get('translation_status') == 'failed')
print(f'最终分段 {len(finals)}，译文完成 {done}，失败 {failed}')
print(f'人工字幕 {len(reference)} 字，识别 {len(spoken)} 字，字符相似度 {ratio:.3f}')
if reference:
    print(f'识别：{spoken[:120]}')
    print(f'字幕：{reference[:120]}')
PY
