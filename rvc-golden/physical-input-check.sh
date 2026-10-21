#!/usr/bin/env bash
# 物理输入检查：只做识别与翻译，不播报。
#
# 播报会经 USB 声卡输出回到同一插头的麦克风输入，形成 TTS → ASR 自环：
# 英文译文被中文识别器按同音字再转写一遍，又产生新的翻译和播报。
# 对录检查必须关闭播放。
#
# 用法：
#   rvc-golden/physical-input-check.sh <repo> <run-dir> [额外参数...]
# 例：
#   rvc-golden/physical-input-check.sh ~/mozart-demo-20261020 /tmp/check-1
# 其余参数原样传给 tools/run_translated_speech.py。
set -euo pipefail

repo="${1:?用法: $0 <repo> <run-dir> [额外参数...]}"
run="${2:?用法: $0 <repo> <run-dir> [额外参数...]}"
shift 2

model_root="${MOZART_MODELS:-$HOME/models/sherpa-onnx/clone-test-downloads}"
final_model="${MOZART_FINAL_MODEL:-$model_root/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17}"
reference="${MOZART_REFERENCE:-$HOME/Mozart/rvc-golden/clone-tts/qiqi-20261004/reference-qiqi-natural-source.wav}"

mkdir -p "$run"
cd "$repo"
exec env PATH="${MOZART_VENV:-$HOME/Mozart/.venv}/bin:$PATH" python3 -u tools/run_translated_speech.py \
    --backend-config state/translated-speech.yaml \
    --mode full \
    --reference "$reference" \
    --final-model "$final_model" \
    --mic-device "${MOZART_MIC:-plughw:CARD=Device,DEV=0}" \
    --no-playback \
    --run-dir "$run" \
    --data-dir "$run/data" \
    "$@"
