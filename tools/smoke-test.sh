#!/usr/bin/env bash
# 冒烟测试：跑一小段输入，检查字幕产出、翻译完成、变速库上限。
#
# 用法：tools/smoke-test.sh [秒数]
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
# 统一用虚拟环境的解释器：子进程继承 sys.executable。
python="${MOZART_PYTHON:-}"
if [ -z "$python" ]; then
    if [ -x "$HOME/Mozart/.venv/bin/python3" ]; then
        python="$HOME/Mozart/.venv/bin/python3"
    else
        python="python3"
    fi
fi
export PATH="$(dirname "$python"):$PATH"
export MOZART_PYTHON="$python"
seconds="${1:-20}"
run="${MOZART_SMOKE_DIR:-/tmp/mozart-smoke}"

echo "== 语音链冒烟（${seconds} 秒输入）"
rm -rf "$run"
cd "$root"
"$python" -u tools/run_translated_speech.py \
    --backend-config state/translated-speech.yaml --mode full \
    --reference "${MOZART_REFERENCE:-$HOME/Mozart/rvc-golden/clone-tts/qiqi-20261004/reference-qiqi-natural-source.wav}" \
    --final-model "${MOZART_FINAL_MODEL:-$HOME/models/sherpa-onnx/clone-test-downloads/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17}" \
    --input "${MOZART_SMOKE_INPUT:-$HOME/mozart-full-soak-input.mp4}" \
    --start "${MOZART_SMOKE_START:-63}" --seconds "$seconds" --no-playback \
    --run-dir "$run" --data-dir "$run/data" > "$run.log" 2>&1

"$python" - "$run/subtitles.jsonl" <<'PY'
import json, sys
rows = {}
for line in open(sys.argv[1]):
    record = json.loads(line)
    rows[record['utterance_id']] = record
finals = [r for r in rows.values() if r.get('final')]
done = [r for r in finals if r.get('translation_status') == 'completed']
if len(finals) < 3:
    raise SystemExit(f'分段太少：{len(finals)}')
if not done:
    raise SystemExit('没有任何译文完成')
print(f'分段 {len(finals)}，译文完成 {len(done)}')
PY

echo "== 变速上限一致性"
"$python" "$root/tools/check-tempo-limit.py" "$root"

echo "== 服务状态"
for name in mozart-usb-audio; do
    if systemctl list-unit-files "$name.service" >/dev/null 2>&1; then
        printf '%s: %s\n' "$name" "$(systemctl is-active "$name" || true)"
    fi
done
echo "冒烟测试通过"
