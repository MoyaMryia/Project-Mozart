#!/bin/bash
# ============================================================================
# Project Mozart · 全栈一键启动 / 停止
#
#   ./start-demo.sh [start|stop|restart|status]     (默认 start)
#
# 拉起顺序：
#   1. mozart_stated        实时后端（rt_rvc + qiqi realtime，HTTP :18080）
#   2. llama-server         Qwen3.5-0.8B 翻译         :18200
#   3. stt_service          sherpa-onnx 流式中文      :18100
#   4. subtitle_bridge      STT → 翻译 → 字幕 JSONL   → 前端 SSE
#   5. mozart-pre 看护       HK MIC 采集 → :18000 → PCM2902 回放
#   6. 前端                 vite preview               :5173
#
# 音频链路：HK MIC(card3) → mozart-pre → 后端 UDP 变声 → PCM2902(card0)
# 所有路径 / 设备 / 端口均可用环境变量覆盖，见下方默认值。
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

# ---- 可覆盖配置 ------------------------------------------------------------
BACKEND_BIN=${BACKEND_BIN:-$REPO/build/gpu/state/mozart_stated}
PRE_BIN=${PRE_BIN:-$REPO/build/gpu/preprocessor/mozart-pre}
BACKEND_YAML=${BACKEND_YAML:-$REPO/build/validation/backend.yaml}
LOG_DIR=${LOG_DIR:-$REPO/build/validation}
RUN_DIR=${RUN_DIR:-$LOG_DIR/run}
MODEL_ID=${MODEL_ID:-qiqi-zh-realtime}

# 音频设备：card0(PCM2902) 的 ALSA id 是 "Device"，card3(HK MIC) 是 "MIC"
MIC_DEV=${MIC_DEV:-plughw:CARD=MIC,DEV=0}
OUT_DEV=${OUT_DEV:-plughw:CARD=Device,DEV=0}
AUDIO_PORT=${AUDIO_PORT:-18000}
CONTROL_PORT=${CONTROL_PORT:-18080}
STT_PORT=${STT_PORT:-18100}
LLAMA_PORT=${LLAMA_PORT:-18200}
FRONTEND_PORT=${FRONTEND_PORT:-5173}

SUBTITLES_JSONL=${SUBTITLES_JSONL:-$LOG_DIR/subtitles.jsonl}
STT_MODEL=${STT_MODEL:-$HOME/models/sherpa-onnx/zipformer-zh-14M}
LLAMA_BIN=${LLAMA_BIN:-$HOME/mozart-archive/qwen35/llama.cpp/build-cuda-bin/llama-server}
LLAMA_MODEL=${LLAMA_MODEL:-$HOME/mozart-archive/qwen35/gguf/qwen35-0.8b-text-Q4_K_M.gguf}

if [ -z "${PY:-}" ]; then
    if [ -x "$HOME/Mozart/.venv/bin/python" ]; then PY="$HOME/Mozart/.venv/bin/python"
    else PY=python3; fi
fi

ENABLE_TEXT=${ENABLE_TEXT:-1}
ENABLE_FRONTEND=${ENABLE_FRONTEND:-1}

# 供 `_supervise-pre` 子进程继承
export MOZART_SUBTITLES_JSONL="$SUBTITLES_JSONL" MIC_DEV OUT_DEV AUDIO_PORT STT_PORT PRE_BIN LOG_DIR

mkdir -p "$LOG_DIR" "$RUN_DIR"
touch "$SUBTITLES_JSONL"

SELF="$REPO/$(basename "${BASH_SOURCE[0]}")"

log()  { printf '[start-demo] %s\n' "$*" >&2; }
pidf() { printf '%s/%s.pid' "$RUN_DIR" "$1"; }
is_up(){ local p; p=$(pidf "$1"); [ -f "$p" ] && kill -0 "$(cat "$p")" 2>/dev/null; }

spawn() {                       # spawn <name> <logfile> <cmd...>
    local name=$1 logf=$2; shift 2
    if is_up "$name"; then
        log "$name 已在运行 (pid $(cat "$(pidf "$name")"))，跳过"
        return 0
    fi
    setsid nohup "$@" >>"$logf" 2>&1 </dev/null &
    echo $! >"$(pidf "$name")"
    log "$name 启动 (pid $(cat "$(pidf "$name")")) → $logf"
}

http_up() { curl -s --max-time 2 "http://127.0.0.1:$CONTROL_PORT/api/status" >/dev/null 2>&1; }

# ---- 看护：HK MIC USB 重新枚举后 mozart-pre 会退出，这里负责重启 -----------
supervise_pre() {
    local logf="$LOG_DIR/mozart-pre-live.log"
    while true; do
        echo "[supervisor] $(date '+%F %T') starting mozart-pre" >>"$logf"
        "$PRE_BIN" -d "$MIC_DEV" -o "$OUT_DEV" \
                   -a 127.0.0.1 -p "$AUDIO_PORT" -b 127.0.0.1:"$STT_PORT" -n 0 \
                   >>"$logf" 2>&1
        local rc=$?
        echo "[supervisor] $(date '+%F %T') mozart-pre exited rc=$rc, retry in 3s" >>"$logf"
        sleep 3
    done
}

# ---- 启动 ------------------------------------------------------------------
start_backend() {
    spawn backend "$LOG_DIR/backend.log" "$BACKEND_BIN" "$BACKEND_YAML"
    log "等待控制面 :$CONTROL_PORT ..."
    local i
    for i in $(seq 1 60); do http_up && break; sleep 1; done
    if ! http_up; then log "后端 HTTP 未就绪，放弃切换模式"; return 1; fi
    log "切换到 rt_rvc / $MODEL_ID"
    curl -s --max-time 90 -X POST "http://127.0.0.1:$CONTROL_PORT/api/mode/switch" \
        -H 'Content-Type: application/json' \
        -d "{\"mode\":\"rt_rvc\",\"model_id\":\"$MODEL_ID\"}" >/dev/null || true
}

start_text_path() {
    [ "$ENABLE_TEXT" = 1 ] || { log "ENABLE_TEXT=0，跳过文字路"; return 0; }
    spawn llama "$LOG_DIR/llama.log" env "LD_LIBRARY_PATH=$(dirname "$LLAMA_BIN")" \
        "$LLAMA_BIN" -m "$LLAMA_MODEL" -c 2048 -ngl 99 --port "$LLAMA_PORT" --host 127.0.0.1
    spawn stt "$LOG_DIR/stt.log" \
        "$PY" "$REPO/tools/stt_service.py" --model "$STT_MODEL" --port "$STT_PORT" --json
    spawn bridge "$LOG_DIR/subtitles.log" \
        "$PY" "$REPO/tools/subtitle_bridge.py" \
        --stt-model "$STT_MODEL" --stt-port "$STT_PORT" \
        --llama-url "http://127.0.0.1:$LLAMA_PORT" --jsonl "$SUBTITLES_JSONL"
}

start_frontend() {
    [ "$ENABLE_FRONTEND" = 1 ] || { log "ENABLE_FRONTEND=0，跳过前端"; return 0; }
    if is_up frontend; then log "frontend 已在运行"; return 0; fi
    (
        cd "$REPO/frontend" || exit 1
        setsid nohup npm run preview -- --host 0.0.0.0 --port "$FRONTEND_PORT" --strictPort \
            >>"$LOG_DIR/frontend.log" 2>&1 </dev/null &
        echo $! >"$(pidf frontend)"
    )
    log "frontend 启动 (pid $(cat "$(pidf frontend)")) → http://<board-ip>:$FRONTEND_PORT"
}

start_all() {
    start_backend
    start_text_path
    spawn pre-supervisor "$LOG_DIR/supervisor.log" "$SELF" _supervise-pre
    start_frontend
    log "完成。查看状态： $0 status"
}

# ---- 停止 ------------------------------------------------------------------
kp() { pkill -f "$1" 2>/dev/null && log "killed: $1" || true; }

stop_all() {
    log "先停看护，避免它把 mozart-pre 又拉起来"
    kp '[r]un-pre-live.sh'
    kp "[s]tart-demo.sh _supervise-pre"
    local n p
    for n in pre-supervisor mozart-pre frontend bridge stt llama backend; do
        p=$(pidf "$n"); [ -f "$p" ] && kill "$(cat "$p")" 2>/dev/null; rm -f "$p"
    done
    sleep 1
    kp '[p]reprocessor/mozart-pre'
    kp '[m]ozart_stated'
    kp '[t]ools/stt_service.py'
    kp '[t]ools/subtitle_bridge.py'
    kp '[l]lama-server'
    kp '[v]ite.*preview'
    kp '[n]pm run preview'
    sleep 1
    log "剩余相关进程："
    pgrep -af "mozart_stated|preprocessor/mozart-pre|llama-server|tools/stt_service.py|tools/subtitle_bridge.py|vite.*preview" >&2 \
        || log "  (无)"
}

status_all() {
    log "进程："
    pgrep -af "mozart_stated|preprocessor/mozart-pre|run-pre-live|_supervise-pre|llama-server|tools/stt_service.py|tools/subtitle_bridge.py|vite.*preview" >&2 || log "  (空)"
    if http_up; then
        curl -s --max-time 3 "http://127.0.0.1:$CONTROL_PORT/api/status" | python3 -c '
import json,sys
d=json.load(sys.stdin); s=d["stream"]
print("[start-demo] mode=%s model=%s blocks=%d errors=%d vad=%.1f%% latency=%.1fms mic_muted=%s"
      % (d["mode"], d["model"]["id"], s["blocks"], s["inference_errors"],
         d["vad"]["voiced_percent"], d["latency"]["avg_ms"], d["realtime"]["mic_muted"]))'
    else
        log "后端未响应 :$CONTROL_PORT"
    fi
    [ -f "$LOG_DIR/mozart-pre-live.log" ] && tail -1 "$LOG_DIR/mozart-pre-live.log" >&2
}

case "${1:-start}" in
    _supervise-pre) supervise_pre ;;
    start)          start_all ;;
    stop)           stop_all ;;
    restart)        stop_all; sleep 2; start_all ;;
    status)         status_all ;;
    *) cat >&2 <<EOF
用法: $0 [start|stop|restart|status]
  start    启动全栈（默认）
  stop     停止全栈
  restart  先停后起
  status   查看进程与后端状态
可覆盖环境变量: MIC_DEV OUT_DEV MODEL_ID CONTROL_PORT AUDIO_PORT STT_PORT LLAMA_PORT FRONTEND_PORT LOG_DIR PY ENABLE_TEXT ENABLE_FRONTEND
EOF
       exit 2 ;;
esac
