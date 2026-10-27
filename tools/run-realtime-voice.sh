#!/usr/bin/env bash
# 启动实时变声链路：USB 麦克风 -> mozart-pre -> RVC 后端 -> USB 声卡。
#
# 用法：
#   tools/run-realtime-voice.sh start     # 启动后端与预处理
#   tools/run-realtime-voice.sh status    # 查看状态
#   tools/run-realtime-voice.sh stop      # 停止
#
# 环境变量覆盖：CONFIG CAPTURE PLAYBACK MODEL PITCH INDEX_RATE INDEX_NPROBE
#               API AUDIO_PORT RUN_DIR
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BACKEND_BIN="$ROOT/build-gpu/state/mozart_stated"
PRE_BIN="$ROOT/build/usb-pre/mozart-pre"

CONFIG=${CONFIG:-$ROOT/rvc-golden/qiqi-zh-run/backend-index.yaml}
CAPTURE=${CAPTURE:-plughw:CARD=MIC,DEV=0}
PLAYBACK=${PLAYBACK:-plughw:CARD=Device,DEV=0}
MODEL=${MODEL:-qiqi-zh}
PITCH=${PITCH:-11}
INDEX_RATE=${INDEX_RATE:-0.5}
INDEX_NPROBE=${INDEX_NPROBE:-16}
API=${API:-http://127.0.0.1:18181}
AUDIO_PORT=${AUDIO_PORT:-18101}
RUN_DIR=${RUN_DIR:-/tmp/mozart-realtime}

BACKEND_PID_FILE="$RUN_DIR/backend.pid"
PRE_PID_FILE="$RUN_DIR/pre.pid"
BACKEND_LOG="$RUN_DIR/backend.log"
PRE_LOG="$RUN_DIR/pre.log"
PRE_STATUS="$RUN_DIR/pre-status.json"
READY_TIMEOUT=${READY_TIMEOUT:-90}

fail() { echo "错误: $*" >&2; exit 1; }

port_busy() {
    ss -lnt 2>/dev/null | awk 'NR>1 {print $4}' | grep -qE "[:.]$1$"
}

wait_http() {
    local deadline=$((SECONDS + READY_TIMEOUT))
    while (( SECONDS < deadline )); do
        if curl -sf --max-time 2 "$API/api/status" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

wait_field() {
    local path=$1 key=$2 want=$3 deadline=$((SECONDS + READY_TIMEOUT))
    while (( SECONDS < deadline )); do
        if python3 -c "
import json,sys
try: d=json.load(open('$path'))
except Exception: sys.exit(1)
sys.exit(0 if str(d.get('$key'))=='$want' else 1)
" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

stop_pidfile() {
    local file=$1 name=$2
    [ -f "$file" ] || return 0
    local pid
    pid=$(cat "$file")
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.5
        done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
        echo "已停止 $name (pid $pid)"
    fi
    rm -f "$file"
}

command_start() {
    [ -x "$BACKEND_BIN" ] || fail "缺少后端二进制 $BACKEND_BIN（先构建 build-gpu）"
    [ -x "$PRE_BIN" ] || fail "缺少预处理二进制 $PRE_BIN（先构建 build/usb-pre）"
    [ -f "$CONFIG" ] || fail "缺少配置 $CONFIG"
    arecord -L 2>/dev/null | grep -qx "$CAPTURE" \
        || fail "采集设备不存在: $CAPTURE（arecord -l 查看）"

    mkdir -p "$RUN_DIR"
    command_stop >/dev/null 2>&1 || true
    if port_busy 18181 || port_busy "$AUDIO_PORT"; then
        fail "端口 18181 或 $AUDIO_PORT 已被占用。先停止旧进程：ss -lntup | grep -E '18101|18181'"
    fi

    echo "启动后端 $MODEL（配置 $CONFIG）"
    "$BACKEND_BIN" "$CONFIG" >"$BACKEND_LOG" 2>&1 &
    echo $! >"$BACKEND_PID_FILE"
    wait_http || { tail -20 "$BACKEND_LOG" >&2; fail "后端在 ${READY_TIMEOUT}s 内未就绪"; }

    curl -sf -X POST "$API/api/mode/switch" -H 'Content-Type: application/json' \
        -d '{"mode":"idle"}' >/dev/null
    curl -sf -X POST "$API/api/mode/switch" -H 'Content-Type: application/json' \
        -d "{\"mode\":\"rt_rvc\",\"model_id\":\"$MODEL\"}" >/dev/null
    curl -sf -X PUT "$API/api/parameters" -H 'Content-Type: application/json' \
        -d "{\"pitch_shift\":$PITCH,\"index_rate\":$INDEX_RATE,\"index_nprobe\":$INDEX_NPROBE}" >/dev/null

    local has_index
    has_index=$(curl -sf "$API/api/status" | python3 -c "import json,sys;print(json.load(sys.stdin)['model'].get('has_index','false'))")
    [ "$has_index" = "true" ] || fail "模型 $MODEL 未加载索引，检查 $CONFIG 与模型目录"

    echo "启动预处理 $CAPTURE -> $AUDIO_PORT -> $PLAYBACK"
    "$PRE_BIN" -d "$CAPTURE" -o "$PLAYBACK" -a 127.0.0.1 -p "$AUDIO_PORT" \
        --mode ai --period-frames 64 --no-denoise \
        --ctl-port 19040 --status-file "$PRE_STATUS" >"$PRE_LOG" 2>&1 &
    echo $! >"$PRE_PID_FILE"
    wait_field "$PRE_STATUS" state ready || {
        tail -20 "$PRE_LOG" >&2
        command_stop >/dev/null 2>&1 || true
        fail "预处理在 ${READY_TIMEOUT}s 内未就绪"
    }

    command_status
    echo
    echo "链路已启动。说话试听；停止用 tools/run-realtime-voice.sh stop"
}

command_status() {
    if [ -f "$BACKEND_PID_FILE" ] && kill -0 "$(cat "$BACKEND_PID_FILE")" 2>/dev/null; then
        curl -sf --max-time 3 "$API/api/status" | python3 -c "
import json,sys
d=json.load(sys.stdin); s=d.get('stream',{}); m=d.get('model',{})
print('后端   mode=%s model=%s index=%s' % (d.get('mode'), m.get('id'), m.get('has_index')))
print('       blocks=%s packets=%s/%s errors=%s late=%s underruns=%s' % (
    s.get('blocks'), s.get('packets_received'), s.get('packets_sent'),
    s.get('inference_errors'), s.get('late_blocks'), s.get('output_underruns')))
" || echo "后端进程在，状态查询失败"
    else
        echo "后端   未运行"
    fi

    if [ -f "$PRE_PID_FILE" ] && kill -0 "$(cat "$PRE_PID_FILE")" 2>/dev/null; then
        python3 - "$PRE_STATUS" <<'PY' || echo "预处理  进程在，状态文件缺失"
import json,sys
d=json.load(open(sys.argv[1]))
u=d.get('processing_us',{})
print('预处理 state=%s mode=%s frames=%s send_errors=%s' % (
    d.get('state'), d.get('mode'), d.get('frames'), d.get('send_errors')))
print('       processing avg=%sus max=%sus' % (u.get('avg'), u.get('max')))
PY
    else
        echo "预处理  未运行"
    fi
}

command_stop() {
    stop_pidfile "$PRE_PID_FILE" "预处理"
    stop_pidfile "$BACKEND_PID_FILE" "后端"
}

case "${1:-}" in
    start) command_start ;;
    stop) command_stop ;;
    status) command_status ;;
    restart) command_stop; command_start ;;
    *) echo "用法: $0 {start|stop|status|restart}" >&2; exit 2 ;;
esac
