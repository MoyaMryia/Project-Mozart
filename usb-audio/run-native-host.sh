#!/usr/bin/env bash
# 本机原生 USB Host 启动器。所有写入限定在仓库 build 下。
set -euo pipefail
SCRIPT=$(realpath -- "${BASH_SOURCE[0]}")
ROLE=host BUILD_NAME=native-host BINARY_NAME=mozart-usb-host
# shellcheck source=usb-audio/native-launch-common.sh
source "$(dirname -- "$SCRIPT")/native-launch-common.sh"
usage() {
    printf '%s\n' \
        "用法：$0 {build|run|start|stop|status|rollback} [原生参数]" \
        'run/start 支持：--input WAV --output WAV --trace-dir DIR --seconds N' \
        '               --periods N --prefill N --target N --legacy --pace' \
        '输出与 trace-dir 必须位于仓库 build/run 下；status-file 由脚本管理。' \
        'MOZART_CAPTURE/MOZART_PLAYBACK：ALSA 设备，默认 hw:CARD=sofhdadsp,DEV=0。' \
        'MOZART_PULSE_SINK：测试 sink 名称，默认使用 PulseAudio 默认 sink。' \
        '文件模式不访问 PulseAudio；stop/rollback 等待 STOP/drain 后恢复旧状态。'
}
ACTION=${1:---help}
case "$ACTION" in
    -h|--help) usage; exit 0 ;;
    build|run|start|stop|status|rollback) shift ;;
    *) usage >&2; exit 2 ;;
esac
CLI_ARGS=("$@")
ARGS=() FILE_MODE=0
if [[ "$ACTION" == run || "$ACTION" == start ]]; then
    while (($#)); do
        case "$1" in
            --input) [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }; FILE_MODE=1; ARGS+=("$1" "$2"); shift 2 ;;
            --output|--trace-dir)
                [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
                artifact=$(realpath -m -- "$2")
                case "$artifact" in "$ROOT/build/run/"*) ;; *) fail "输出路径必须位于仓库 build/run：$artifact" ;; esac
                ARGS+=("$1" "$artifact"); shift 2 ;;
            --seconds|--periods|--prefill|--target)
                [[ $# -ge 2 ]] || { usage >&2; exit 2; }; ARGS+=("$1" "$2"); shift 2 ;;
            --legacy|--pace) ARGS+=("$1"); shift ;;
            *) usage >&2; exit 2 ;;
        esac
    done
    if [[ "$FILE_MODE" == 0 ]]; then
        ARGS+=(--capture "${MOZART_CAPTURE:-hw:CARD=sofhdadsp,DEV=0}" \
               --playback "${MOZART_PLAYBACK:-hw:CARD=sofhdadsp,DEV=0}")
    fi
    ARGS+=(--status-file "$STATUS")
elif (($#)); then
    usage >&2; exit 2
fi
build_native() {
    cmake -S "$ROOT/usb-audio/native-host" -B "$ROOT/build/native-host" \
        -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
    cmake --build "$ROOT/build/native-host" --parallel 2
}
dispatch
