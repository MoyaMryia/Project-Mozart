#!/usr/bin/env bash
# 一键拉起演示链路：USB 音频桥 → 实时栈（后端、字幕、翻译、预处理）。
#
# 用法：
#   tools/bring-up.sh start     启动桥与实时栈（默认）
#   tools/bring-up.sh stop      停止实时栈与桥
#   tools/bring-up.sh status    显示桥与栈状态
#
# 角色自动判断：aarch64 视为 Jetson，其他视为主机；可用 ROLE=host|jetson 覆盖。
# 环境变量：CAPTURE PLAYBACK OUTPUT BRIDGE_UNIT READY_TIMEOUT MOZART_STATUS_FILE
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ROLE=${ROLE:-$([ "$(uname -m)" = aarch64 ] && echo jetson || echo host)}
BRIDGE_UNIT=${BRIDGE_UNIT:-mozart-usb-audio}
STATUS_FILE=${MOZART_STATUS_FILE:-/run/mozart-usb-audio/status.json}
READY_TIMEOUT=${READY_TIMEOUT:-60}
OUTPUT=${OUTPUT:-voice}

log() { printf '[bring-up] %s\n' "$*"; }
die() { printf '[bring-up] 错误: %s\n' "$*" >&2; exit 1; }

need_aloop() {
    if ! lsmod | grep -q '^snd_aloop'; then
        log "加载 snd-aloop"
        sudo modprobe snd-aloop
    fi
}

bridge_state() {
    python3 - "$STATUS_FILE" <<'PY'
import json, sys, time
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print('missing'); raise SystemExit
age = time.time() - float(data.get('updated_at', 0))
print('active' if data.get('active') is True and age < 15 else 'inactive')
PY
}

wait_bridge() {
    local waited=0
    while [ "$waited" -lt "$READY_TIMEOUT" ]; do
        [ "$(bridge_state)" = active ] && return 0
        sleep 2; waited=$((waited + 2))
    done
    return 1
}

pick_devices() {
    [ -n "${CAPTURE:-}" ] && [ -n "${PLAYBACK:-}" ] && return 0
    local names
    names=$(aplay -L 2>/dev/null || true)
    if grep -qx 'plughw:CARD=MIC,DEV=0' <<<"$names" && grep -qx 'plughw:CARD=Device,DEV=0' <<<"$names"; then
        CAPTURE=${CAPTURE:-plughw:CARD=MIC,DEV=0}
        PLAYBACK=${PLAYBACK:-plughw:CARD=Device,DEV=0}
    else
        log "未找到 MIC／Device 声卡，改用 USB 桥设备"
        CAPTURE=${CAPTURE:-plughw:CARD=Loopback,DEV=1,SUBDEV=0}
        PLAYBACK=${PLAYBACK:-plughw:CARD=Loopback,DEV=0,SUBDEV=1}
    fi
    export CAPTURE PLAYBACK
}

start_bridge() {
    need_aloop
    log "启动 $BRIDGE_UNIT（$ROLE）"
    sudo systemctl start "$BRIDGE_UNIT" || die "无法启动 $BRIDGE_UNIT"
    wait_bridge || die "桥未就绪：检查对端守护进程与 USB 连接（$STATUS_FILE）"
    log "桥已就绪：$(bridge_state)"
}

start_stack() {
    pick_devices
    log "实时栈：$CAPTURE → $PLAYBACK，OUTPUT=$OUTPUT"
    timeout 240 "$ROOT/tools/run-realtime-voice.sh" start >/tmp/bring-up.log 2>&1 \
        || { tail -20 /tmp/bring-up.log >&2; die "实时栈启动失败"; }
    sleep 2
    "$ROOT/tools/run-realtime-voice.sh" status
}

stop_all() {
    if [ "$ROLE" = jetson ]; then
        timeout 60 "$ROOT/tools/run-realtime-voice.sh" stop >/dev/null 2>&1 || true
    fi
    sudo systemctl stop "$BRIDGE_UNIT" 2>/dev/null || true
    log "已停止"
}

status() {
    printf '角色      %s\n' "$ROLE"
    printf '桥        %s（%s）\n' "$(bridge_state)" "$STATUS_FILE"
    if [ "$ROLE" = jetson ]; then
        "$ROOT/tools/run-realtime-voice.sh" status || true
    fi
}

case ${1:-start} in
    start)  start_bridge; [ "$ROLE" = jetson ] && start_stack; log "完成" ;;
    stop)   stop_all ;;
    status) status ;;
    *)      die "用法：$0 [start|stop|status]" ;;
esac
