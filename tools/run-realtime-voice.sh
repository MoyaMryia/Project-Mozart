#!/usr/bin/env bash
# 启动实时变声链路：USB 麦克风 -> mozart-pre -> RVC 后端 -> USB 声卡。
#
# 用法：
#   tools/run-realtime-voice.sh start     # 启动后端与预处理
#   tools/run-realtime-voice.sh status    # 查看状态
#   tools/run-realtime-voice.sh stop      # 停止
#   tools/run-realtime-voice.sh devices   # 列出采集与播放设备（JSON）
#   tools/run-realtime-voice.sh pre-restart [--capture 设备] [--playback 设备]
#                                         # 只重启预处理，切换输入输出设备
#
# 环境变量覆盖：CONFIG CAPTURE PLAYBACK MODEL PITCH INDEX_RATE INDEX_NPROBE
#               API AUDIO_PORT RUN_DIR SUBTITLE_TARGET
# SUBTITLE_TARGET=127.0.0.1:18100 将同一采集帧同时送给字幕识别。
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
PRE_DEVICES="$RUN_DIR/pre-devices.json"
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
    local path=$1 key=$2 want=$3 pid=$4 deadline=$((SECONDS + READY_TIMEOUT))
    while (( SECONDS < deadline )); do
        if python3 - "$path" "$key" "$want" "$pid" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except (FileNotFoundError, json.JSONDecodeError):
    sys.exit(1)
if d.get('pid') == int(sys.argv[4]) and d.get('state') == 'failed':
    print('预处理启动失败: ' + d.get('reason', ''), file=sys.stderr)
    sys.exit(2)
sys.exit(0 if str(d.get(sys.argv[2])) == sys.argv[3] and d.get('pid') == int(sys.argv[4]) else 1)
PY
        then
            return 0
        else
            local result=$?
            [ "$result" -eq 2 ] && return 1
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

    start_pre

    command_status
    echo
    echo "链路已启动。说话试听；停止用 tools/run-realtime-voice.sh stop"
}

# 用当前 CAPTURE/PLAYBACK 起预处理，并把设备写进 pre-devices.json 供控制面读取。
start_pre() {
    echo "启动预处理 $CAPTURE -> $AUDIO_PORT -> $PLAYBACK"
    if [ -f "$PRE_STATUS" ]; then
        mv "$PRE_STATUS" "$PRE_STATUS.previous"
    fi
    local extra=()
    if [ -n "${SUBTITLE_TARGET:-}" ]; then
        extra=(-b "$SUBTITLE_TARGET")
    fi
    setsid "$PRE_BIN" -d "$CAPTURE" -o "$PLAYBACK" -a 127.0.0.1 -p "$AUDIO_PORT" "${extra[@]}" \
        --mode ai --period-frames 64 --no-denoise \
        --ctl-port 19040 --status-file "$PRE_STATUS" >"$PRE_LOG" 2>&1 &
    local pre_pid=$!
    echo "$pre_pid" >"$PRE_PID_FILE"
    wait_field "$PRE_STATUS" state ready "$pre_pid" || {
        tail -20 "$PRE_LOG" >&2
        fail "预处理在 ${READY_TIMEOUT}s 内未就绪"
    }
    write_pre_devices
}

write_pre_devices() {
    python3 - "$CAPTURE" "$PLAYBACK" "${SUBTITLE_TARGET:-}" "$PRE_DEVICES" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[4])
temporary = path.with_suffix('.json.tmp')
temporary.write_text(json.dumps(dict(zip(('capture', 'playback', 'subtitle_target'), sys.argv[1:4]))))
temporary.replace(path)
PY
}

# 列出 ALSA 采集与播放设备。输出一行 JSON，供控制面渲染下拉框。
command_devices() {
    python3 - <<'PY'
import json, re, subprocess

LINE = re.compile(r'^card (\d+): (\S+) \[(.*?)\], device (\d+): (.*?) \[(.*?)\]')

def collect(command):
    output = subprocess.run(command, capture_output=True, text=True).stdout
    items = []
    for line in output.splitlines():
        match = LINE.match(line)
        if not match:
            continue
        card, short, long_name, device, _, pcm = match.groups()
        items.append({
            'id': f'plughw:CARD={short},DEV={device}',
            'card': int(card),
            'label': f'{long_name} / {pcm}' if pcm else long_name,
        })
    return items

print(json.dumps({'capture': collect(['arecord', '-l']),
                  'playback': collect(['aplay', '-l'])}))
PY
}

# 只重启预处理。后端与 stated 保持运行，设备切换不需要重建推理会话。
command_pre_restart() {
    # 控制面只传设备参数。重启时保留原有字幕目标，显式环境变量可覆盖或清空。
    if [ "${SUBTITLE_TARGET+x}" != x ] && [ -f "$PRE_DEVICES" ]; then
        SUBTITLE_TARGET=$(python3 - "$PRE_DEVICES" <<'PY'
import json, sys
print(json.load(open(sys.argv[1])).get('subtitle_target', ''))
PY
)
    fi
    while [ $# -gt 0 ]; do
        case "$1" in
            --capture) [ $# -ge 2 ] || fail "--capture 缺少设备"; CAPTURE=$2; shift 2 ;;
            --playback) [ $# -ge 2 ] || fail "--playback 缺少设备"; PLAYBACK=$2; shift 2 ;;
            *) fail "未知参数 $1" ;;
        esac
    done
    [ -x "$PRE_BIN" ] || fail "缺少预处理二进制 $PRE_BIN（先构建 build/usb-pre）"
    arecord -L 2>/dev/null | grep -qx "$CAPTURE" \
        || fail "采集设备不存在: $CAPTURE"
    aplay -L 2>/dev/null | grep -qx "$PLAYBACK" \
        || fail "播放设备不存在: $PLAYBACK"
    mkdir -p "$RUN_DIR"
    stop_pidfile "$PRE_PID_FILE" "预处理"
    start_pre
    printf '{"status":"active","capture":"%s","playback":"%s"}\n' "$CAPTURE" "$PLAYBACK"
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
    devices) command_devices ;;
    pre-restart) shift; command_pre_restart "$@" ;;
    *) echo "用法: $0 {start|stop|status|restart|devices|pre-restart [--capture 设备] [--playback 设备]}" >&2; exit 2 ;;
esac
