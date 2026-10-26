#!/usr/bin/env bash
# 由 run-native-host.sh 和 run-native-pre.sh 引用。
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"
RUN="$ROOT/build/run/usb-native-$ROLE"
BIN="$ROOT/build/$BUILD_NAME/$BINARY_NAME"

fail() { printf '%s\n' "$*" >&2; return 1; }
repo_build_path() {
    local path
    path=$(realpath -m -- "$1")
    case "$path" in "$ROOT/build/"*) printf '%s\n' "$path" ;; *) fail "路径超出仓库 build：$path" ;; esac
}
# 在创建目录前解析符号链接。
RUN=$(repo_build_path "$RUN")
BIN=$(repo_build_path "$BIN")
PIDFILE="$RUN/native.pid"
STATUS="$RUN/status.json"
LOCK="$RUN/run.lock"
NATIVE_WAIT_SECONDS=15
WAIT_STEPS=$((NATIVE_WAIT_SECONDS * 10))
CHILD=

limited() { timeout --kill-after=2s 10s "$@"; }
privileged() { limited sudo -n "$@"; }
# 只接受本仓库二进制的 argv[0]，并排除已退出的进程。
native_pid() {
    local first
    [[ -r "$PIDFILE" ]] || return 1
    read -r PID < "$PIDFILE"
    [[ "$PID" =~ ^[1-9][0-9]*$ && -r "/proc/$PID/cmdline" ]] || return 1
    IFS= read -r -d '' first < "/proc/$PID/cmdline" || return 1
    [[ "$first" == "$BIN" ]]
}
process_exists() { [[ ${PID:-} =~ ^[1-9][0-9]*$ && -d /proc/$PID ]]; }
wait_native() {
    local i
    for ((i=0; i<WAIT_STEPS; i++)); do
        if ! native_pid && ! process_exists; then return 0; fi
        sleep 0.1
    done
    fail "原生进程未在 $NATIVE_WAIT_SECONDS s 内退出：$PID；保留恢复记录，请重试 rollback。"
}
restore_host() {
    local rc=0 sink
    if [[ -f "$RUN/pulse.restore" ]]; then
        read -r sink < "$RUN/pulse.restore"
        if limited pactl suspend-sink "$sink" 0; then
            rm -- "$RUN/pulse.restore"
        else
            printf 'PulseAudio 恢复失败：%s\n' "$sink" >&2
            rc=1
        fi
    fi
    if [[ -f "$RUN/legacy.restore" ]]; then
        if privileged systemctl start mozart-usb-audio.service; then
            rm -- "$RUN/legacy.restore"
        else
            printf '旧服务恢复失败。\n' >&2
            rc=1
        fi
    fi
    return "$rc"
}
restore() { if [[ "$ROLE" == host ]]; then restore_host; fi; }
cleanup() {
    local rc=$? cleanup_rc=0
    trap - EXIT
    trap '' INT TERM HUP
    # 信号可能早于 sudo 写入 PID 文件。先等待 exec 或 sudo 退出。
    local i
    if [[ -n "$CHILD" ]]; then
        for ((i=0; i<WAIT_STEPS; i++)); do
            native_pid && break
            kill -0 "$CHILD" 2>/dev/null || break
            sleep 0.1
        done
    fi
    if native_pid; then
        privileged kill -TERM "$PID" || cleanup_rc=1
        wait_native || cleanup_rc=1
    elif [[ -n "$CHILD" ]] && kill -0 "$CHILD" 2>/dev/null; then
        printf 'sudo 子进程尚未完成 exec；保留恢复记录。\n' >&2
        printf '清理失败；请运行 rollback。\n' > "$RUN/recovery-error.log"
        exit 1
    fi
    if process_exists; then
        printf 'PID 尚未退出；恢复记录保留在 %s。\n' "$RUN" >&2
        cleanup_rc=1
    else
        restore || cleanup_rc=1
        rm -f -- "$PIDFILE" "$RUN/supervisor.pid"
    fi
    if ((cleanup_rc)); then
        printf '清理失败；请运行 rollback。\n' > "$RUN/recovery-error.log"
        rc=1
    else
        rm -f -- "$RUN/recovery-error.log"
    fi
    exit "$rc"
}
prepare_host() {
    local state rc sink listing sink_state
    # 查询失败与 inactive 分开处理，避免丢失旧服务状态。
    if state=$(limited systemctl is-active mozart-usb-audio.service); then
        [[ "$state" == active ]] || fail "旧服务状态异常：$state"
        : > "$RUN/legacy.restore"
        privileged systemctl stop mozart-usb-audio.service
    else
        rc=$?
        [[ "$rc" == 3 && "$state" == inactive ]] || fail "旧服务状态查询失败：$state ($rc)"
    fi
    [[ "$FILE_MODE" == 0 ]] || return 0
    sink=${MOZART_PULSE_SINK:-}
    if [[ -z "$sink" ]]; then sink=$(limited pactl get-default-sink); fi
    listing=$(limited pactl list short sinks)
    sink_state=$(awk -v name="$sink" '$2 == name {print $NF}' <<< "$listing")
    [[ -n "$sink_state" ]] || fail "找不到 PulseAudio 测试 sink：$sink"
    if [[ "$sink_state" != SUSPENDED ]]; then
        printf '%s\n' "$sink" > "$RUN/pulse.restore"
        limited pactl suspend-sink "$sink" 1
    fi
}
prepare_pre() {
    local rc endpoint
    for endpoint in /dev/ffs-mozart/ep3 /dev/ffs-mozart/ep4; do
        privileged test -e "$endpoint" || fail "专用 USB 端点不存在：$endpoint"
        if privileged fuser "$endpoint"; then
            fail "专用 USB 端点已被其它进程占用：$endpoint"
        else
            rc=$?
            [[ "$rc" == 1 ]] || fail "USB 端点占用查询失败：$endpoint ($rc)"
        fi
    done
    python3 -c 'import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind(("127.0.0.1",19040))'
}
run_native() {
    mkdir -p -- "$RUN"
    exec 9>"$LOCK"
    flock -n 9 || fail "本仓库 $ROLE 启动器已在运行。"
    if native_pid; then fail "本仓库原生进程已在运行：$PID"; fi
    if process_exists; then fail "PID 文件指向未知进程：$PID；未发送信号。"; fi
    [[ -x "$BIN" ]] || fail "缺少原生程序，请先运行 build：$BIN"
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP
    restore
    rm -f -- "$PIDFILE" "$STATUS" "$STATUS.tmp"
    printf '%s\n' "$$" > "$RUN/supervisor.pid"
    if [[ "$ROLE" == host ]]; then prepare_host; else prepare_pre; fi
    # sudo 子进程关闭锁；监督 shell 持锁直至恢复结束。
    sudo -n sh -c 'pidfile=$1; shift; printf "%s\n" "$$" > "$pidfile" || exit 1; exec chrt --fifo 40 "$@"' \
        mozart-native "$PIDFILE" "$BIN" "${ARGS[@]}" 9>&- &
    CHILD=$!
    local rc
    if wait "$CHILD"; then rc=0; else rc=$?; fi
    exit "$rc"
}
start_native() {
    mkdir -p -- "$RUN"
    # 日志重定向由普通用户完成；root 只执行原生程序。
    nohup "$SCRIPT" run "${CLI_ARGS[@]}" >> "$RUN/native.log" 2>&1 < /dev/null &
    local child=$! i owner
    for ((i=0; i<WAIT_STEPS; i++)); do
        if ! kill -0 "$child" 2>/dev/null; then
            if wait "$child"; then fail "原生程序在启动时退出；请查看 $RUN/native.log"; else return $?; fi
        fi
        if [[ -r "$RUN/supervisor.pid" ]] && read -r owner 2>/dev/null < "$RUN/supervisor.pid"; then
            :
        else
            owner=
        fi
        if [[ "$owner" == "$child" ]] && native_pid; then
            sleep 0.3
            if native_pid; then printf '已启动 PID=%s；日志：%s/native.log\n' "$PID" "$RUN"; return 0; fi
        fi
        sleep 0.1
    done
    # 超时只通知刚创建的监督 shell，由其 trap 执行有界恢复。
    kill -TERM "$child" 2>/dev/null || true
    fail "启动超时；请查看 $RUN/native.log，并运行 rollback。"
}
stop_native() {
    [[ -d "$RUN" ]] || { printf '未启动。\n'; return 0; }
    if native_pid; then
        privileged kill -TERM "$PID"
        wait_native
    elif process_exists; then
        fail "PID 文件指向未知进程：$PID；未发送信号。"
    fi
    exec 9>"$LOCK"
    # 在启动期间继续检查原生 PID，覆盖 PID 发布前的停止请求。
    local deadline=$((SECONDS + 30))
    until flock -n 9; do
        if native_pid; then
            privileged kill -TERM "$PID"
            wait_native
        elif process_exists; then
            fail "PID 文件指向未知进程：$PID；未发送信号。"
        fi
        ((SECONDS < deadline)) || fail "监督 shell 尚未完成清理；请重试 rollback。"
        sleep 0.1
    done
    # 监督 shell 异常退出时，原生进程可能仍保留 PID 文件。
    if native_pid; then
        privileged kill -TERM "$PID"
        wait_native
    elif process_exists; then
        fail "PID 文件指向未知进程：$PID；未发送信号。"
    fi
    restore
    rm -f -- "$PIDFILE" "$RUN/supervisor.pid" "$RUN/recovery-error.log"
    printf '已停止，已完成恢复。\n'
}
show_status() {
    if native_pid; then
        printf 'process=running pid=%s binary=%s\n' "$PID" "$BIN"
    elif process_exists; then
        printf 'process=unknown pid=%s（未发送信号）\n' "$PID"
    else
        printf 'process=stopped\n'
    fi
    if [[ -f "$STATUS" ]]; then cat -- "$STATUS"; printf '\n'; fi
    if [[ -f "$RUN/recovery-error.log" ]]; then cat -- "$RUN/recovery-error.log"; fi
    if [[ -f "$RUN/legacy.restore" || -f "$RUN/pulse.restore" ]]; then printf 'restore=pending\n'; fi
}
dispatch() {
    case "$ACTION" in
        build) build_native ;;
        run) run_native ;;
        start) start_native ;;
        stop|rollback) stop_native ;;
        status) show_status ;;
    esac
}
