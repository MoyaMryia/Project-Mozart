#!/usr/bin/env bash
# 演示链路一键启动：USB 桥 → 后端、字幕、翻译、预处理 → 控制台 → 译文播报。
#
# 用法：
#   tools/demo-up.sh start      启动整条链路并切到译文播报
#   tools/demo-up.sh stop       停止控制台与实时栈
#   tools/demo-up.sh status     显示桥、栈、控制台与播报状态
#
# 环境变量：REFERENCE VOICE_NAME SPEECH_LANGUAGE CONSOLE_HOST CONSOLE_PORT
#           API RUN_DIR READY_TIMEOUT CAPTURE PLAYBACK MODEL PITCH
# 其余变量与 tools/bring-up.sh、tools/run-realtime-voice.sh 相同。
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
API=${API:-http://127.0.0.1:18181}
CONSOLE_HOST=${CONSOLE_HOST:-0.0.0.0}
CONSOLE_PORT=${CONSOLE_PORT:-8080}
CONSOLE_PID_FILE=${CONSOLE_PID_FILE:-/tmp/mozart-realtime/console.pid}
CONSOLE_LOG=${CONSOLE_LOG:-/tmp/mozart-realtime/console.log}
RUN_DIR=$(dirname "$CONSOLE_PID_FILE")
REFERENCE=${REFERENCE:-$HOME/models/reference/default.wav}
VOICE_NAME=${VOICE_NAME:-Mozart 演示音色}
SPEECH_LANGUAGE=${SPEECH_LANGUAGE:-en}
READY_TIMEOUT=${READY_TIMEOUT:-180}
PYTHON=${PYTHON:-python3}
API_HOSTPORT=${API#http://}

log() { printf '[demo] %s\n' "$*"; }
die() { printf '[demo] 错误: %s\n' "$*" >&2; exit 1; }

# 参考音频是译文播报的克隆源。长度范围由 tools/run-realtime-voice.sh 校验。
require_reference() {
    [ -f "$REFERENCE" ] || die "缺少参考音频 $REFERENCE。放一段 3-15 s 语音，或设置 REFERENCE"
}

lan_address() {
    hostname -I 2>/dev/null | awk '{print $1}'
}

# 读 /api/stack/status。退出码 0 表示链路已就绪：字幕在采集、翻译与播报已就绪。
stack_ready() {
    "$PYTHON" - "$API" <<'PY'
import json, sys, urllib.request

try:
    with urllib.request.urlopen(sys.argv[1] + '/api/stack/status', timeout=5) as response:
        data = json.load(response)
except Exception as error:
    print('状态不可读: ' + type(error).__name__, file=sys.stderr)
    sys.exit(1)
readiness = data.get('readiness') or {}
if data.get('state') != 'running' or data.get('phase') != 'capturing':
    print('state=%s phase=%s' % (data.get('state'), data.get('phase')), file=sys.stderr)
    sys.exit(1)
missing = [name for name in ('translation', 'speech_runtime', 'voice_session') if not readiness.get(name)]
if missing:
    print('未就绪: ' + ', '.join(missing), file=sys.stderr)
    sys.exit(1)
PY
}

wait_ready() {
    local deadline=$((SECONDS + READY_TIMEOUT)) reason
    while (( SECONDS < deadline )); do
        if reason=$(stack_ready 2>&1); then
            return 0
        fi
        sleep 2
    done
    printf '[demo] 链路未就绪（%s）。最后原因：%s\n' "$READY_TIMEOUT s" "${reason:-无}" >&2
    return 1
}

wait_console() {
    local deadline=$((SECONDS + 20))
    while (( SECONDS < deadline )); do
        curl -sf --max-time 2 "http://127.0.0.1:$CONSOLE_PORT/" >/dev/null 2>&1 && return 0
        sleep 0.5
    done
    return 1
}

console_pid() {
    [ -f "$CONSOLE_PID_FILE" ] || return 1
    local pid
    pid=$(cat "$CONSOLE_PID_FILE" 2>/dev/null) || return 1
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    grep -q 'serve-console.py' "/proc/$pid/cmdline" 2>/dev/null || return 1
    printf '%s' "$pid"
}

stop_console() {
    local pid
    if pid=$(console_pid); then
        log "停止控制台（$pid）"
        kill "$pid" 2>/dev/null || true
        sleep 1
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$CONSOLE_PID_FILE"
}

start_console() {
    mkdir -p "$RUN_DIR"
    stop_console
    log "启动控制台 $CONSOLE_HOST:$CONSOLE_PORT，接口转发 $API_HOSTPORT"
    setsid "$PYTHON" "$ROOT/tools/serve-console.py" --host "$CONSOLE_HOST" \
        --port "$CONSOLE_PORT" --api "$API_HOSTPORT" >"$CONSOLE_LOG" 2>&1 </dev/null &
    echo $! >"$CONSOLE_PID_FILE"
    wait_console || {
        tail -5 "$CONSOLE_LOG" >&2
        die "控制台未监听 $CONSOLE_PORT，日志 $CONSOLE_LOG"
    }
}

# 播报需要已注册的音色。带 REFERENCE 启动时，栈注册并选中同名音色。
# 每次启动新建一份参考音色，这里删掉同名旧副本，避免音色列表增长。
enable_speech() {
    "$PYTHON" - "$API" "$VOICE_NAME" "$SPEECH_LANGUAGE" <<'PY'
import json, sys, urllib.error, urllib.request

api, name, language = sys.argv[1], sys.argv[2], sys.argv[3]

def call(method, path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(api + path, data=body, method=method,
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)

status = call('GET', '/api/speech/status')
session = status.get('session') or {}
voices = call('GET', '/api/voices').get('voices') or []
mine = [voice for voice in voices if voice.get('name') == name]
if not mine:
    raise SystemExit('未找到音色 %r。启动时传 REFERENCE，或在控制台注册' % name)
active = session.get('voice_id')
chosen = active if active in [voice['id'] for voice in mine] else mine[0]['id']
if not session.get('enabled') or active != chosen:
    call('POST', '/api/speech/session',
         {'enabled': True, 'voice_id': chosen, 'language': language, 'playback': True})
    print('播报会话已启用：%s' % chosen)
else:
    print('播报会话已就绪：%s' % chosen)
removed = 0
for voice in mine:
    if voice['id'] == chosen:
        continue
    try:
        call('DELETE', '/api/voices/' + voice['id'])
        removed += 1
    except urllib.error.HTTPError as error:
        print('保留旧音色 %s：HTTP %s' % (voice['id'], error.code), file=sys.stderr)
if removed:
    print('已清理同名旧音色 %d 份' % removed)
PY
}

start_all() {
    require_reference
    log "启动桥与实时栈，输出译文播报（参考音频 $REFERENCE）"
    OUTPUT=both REFERENCE="$REFERENCE" REFERENCE_NAME="$VOICE_NAME" \
        "$ROOT/tools/bring-up.sh" start
    wait_ready || die "链路未就绪：查看 $RUN_DIR 与最新的 ~/mozart-archive/logs/realtime-* 日志"
    enable_speech || log "播报会话未启用：在控制台选择音色后点“启动翻译”"
    start_console
    log "控制台 http://127.0.0.1:$CONSOLE_PORT/"
    [ -z "$(lan_address)" ] || log "其他主机 http://$(lan_address):$CONSOLE_PORT/"
    log "当前输出：译文播报。控制台可切换到变声或直出"
}

stop_all() {
    stop_console
    "$ROOT/tools/bring-up.sh" stop
}

status_all() {
    "$ROOT/tools/bring-up.sh" status || true
    local pid
    if pid=$(console_pid); then
        printf '控制台    running（%s）http://%s:%s/\n' "$pid" "$CONSOLE_HOST" "$CONSOLE_PORT"
    else
        printf '控制台    stopped\n'
    fi
    curl -sf --max-time 3 "$API/api/speech/status" >/dev/null 2>&1 \
        || { printf '播报      不可读（%s/api/speech/status）\n' "$API"; return 0; }
    "$PYTHON" - "$API" <<'PY'
import json, sys, urllib.request
with urllib.request.urlopen(sys.argv[1] + '/api/speech/status', timeout=5) as response:
    data = json.load(response)
session = data.get('session') or {}
print('播报      session=%s voice=%s' % (bool(session.get('enabled')), session.get('voice_id') or '-'))
PY
}

case ${1:-start} in
    start)  start_all ;;
    stop)   stop_all ;;
    status) status_all ;;
    *)      die "用法：$0 [start|stop|status]" ;;
esac
