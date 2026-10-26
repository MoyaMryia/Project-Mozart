#!/usr/bin/env bash
# 在 Jetson 仓库中运行；保留 gadget daemon0 和 ep0。
set -euo pipefail
SCRIPT=$(realpath -- "${BASH_SOURCE[0]}")
ROLE=pre BUILD_NAME=usb-pre BINARY_NAME=mozart-pre
# shellcheck source=usb-audio/native-launch-common.sh
source "$(dirname -- "$SCRIPT")/native-launch-common.sh"
usage() {
    printf '%s\n' \
        "用法：$0 {build|run|start|stop|status|rollback} [原生参数]" \
        'run/start 支持：-n N --period-frames N --fade-ms N --no-denoise --gate --agc --limiter' \
        '固定参数：--usb-endpoint /dev/ffs-mozart --mode direct --ctl-port 19040。' \
        'MOZART_PRE_PLAYBACK：可选的额外 ALSA 设备；默认只使用 USB。' \
        'stop/rollback 只停止本仓库原生进程；gadget daemon0 保持原状态。'
}
ACTION=${1:---help}
case "$ACTION" in
    -h|--help) usage; exit 0 ;;
    build|run|start|stop|status|rollback) shift ;;
    *) usage >&2; exit 2 ;;
esac
CLI_ARGS=("$@")
ARGS=()
if [[ "$ACTION" == run || "$ACTION" == start ]]; then
    while (($#)); do
        case "$1" in
            -n|--period-frames|--fade-ms)
                [[ $# -ge 2 ]] || { usage >&2; exit 2; }; ARGS+=("$1" "$2"); shift 2 ;;
            --no-denoise|--gate|--agc|--limiter) ARGS+=("$1"); shift ;;
            *) usage >&2; exit 2 ;;
        esac
    done
    ARGS+=(--usb-endpoint /dev/ffs-mozart --mode direct --status-file "$STATUS" \
           --ctl-port 19040)
    if [[ -n ${MOZART_PRE_PLAYBACK:-} ]]; then ARGS+=(-o "$MOZART_PRE_PLAYBACK"); fi
elif (($#)); then
    usage >&2; exit 2
fi
build_native() {
    cmake -S "$ROOT/preprocessor" -B "$ROOT/build/usb-pre" \
        -DCMAKE_BUILD_TYPE=Release -DMOZART_USE_RNNOISE=OFF -DMOZART_BUILD_TESTS=ON
    cmake --build "$ROOT/build/usb-pre" --parallel 2
}
dispatch
