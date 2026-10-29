#!/usr/bin/env bash
# 启动实时变声、中文字幕和英文翻译，共用一次麦克风采集。
#
# 用法：
#   tools/run-realtime-voice.sh start     # 启动后端、字幕、翻译和预处理
#   tools/run-realtime-voice.sh status    # 查看状态
#   tools/run-realtime-voice.sh stop      # 停止
#   tools/run-realtime-voice.sh devices   # 列出采集与播放设备（JSON）
#   tools/run-realtime-voice.sh pre-restart [--capture 设备] [--playback 设备]
#                                         # 只重启预处理，切换输入输出设备
#
# 环境变量覆盖：CONFIG CAPTURE PLAYBACK MODEL PITCH INDEX_RATE INDEX_NPROBE
#               API AUDIO_PORT RUN_DIR PYTHON CAPTIONS_DIR
#               STT_MODEL FINAL_MODEL LLAMA_SERVER LLAMA_MODEL GLOSSARY CORRECTIONS
#               OUTPUT REFERENCE REFERENCE_NAME SPEECH_EARLY_AUDIO TTS_PYTHONPATH
#               BRIDGE_CAPTURE BRIDGE_PLAYBACK MOZART_STATUS_FILE BRIDGE_STALE_SECONDS
# OUTPUT=voice 输出变声音频；OUTPUT=speech 输出译文播报；OUTPUT=both 两者服务都运行，
# 由 stdout 控制台切换当前输出。
# API 默认 18181；字幕音频固定送往本机 18100；翻译使用本机 18200。
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BACKEND_BIN="$ROOT/build-gpu/state/mozart_stated"
PRE_BIN="$ROOT/build/usb-pre/mozart-pre"
PYTHON=${PYTHON:-$ROOT/.venv/bin/python}
STT_MODEL=${STT_MODEL:-$HOME/models/sherpa-onnx/zipformer-zh-14M}
FINAL_MODEL=${FINAL_MODEL-$HOME/models/sherpa-onnx/clone-test-downloads/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17}
LLAMA_SERVER=${LLAMA_SERVER:-$HOME/mozart-archive/qwen35/llama.cpp/build-cuda-bin/llama-server}
LLAMA_MODEL=${LLAMA_MODEL:-$HOME/mozart-archive/qwen35/gguf/qwen35-0.8b-text-Q4_K_M.gguf}
# 术语表默认写到 state/glossary.json，供控制台读写；设 GLOSSARY= 关闭。
GLOSSARY=${GLOSSARY-${MOZART_GLOSSARY-$ROOT/state/glossary.json}}
CORRECTIONS=${CORRECTIONS:-${MOZART_CORRECTIONS:-}}

CONFIG=${CONFIG:-$ROOT/rvc-golden/qiqi-zh-run/backend-index.yaml}
CAPTURE=${CAPTURE:-plughw:CARD=MIC,DEV=0}
PLAYBACK=${PLAYBACK:-plughw:CARD=Device,DEV=0}
OUTPUT=${OUTPUT:-voice}
REFERENCE=${REFERENCE:-}
REFERENCE_NAME=${REFERENCE_NAME:-Reference voice}
SPEECH_EARLY_AUDIO=${SPEECH_EARLY_AUDIO:-0}
TTS_PYTHONPATH=${TTS_PYTHONPATH:-}
MODEL=${MODEL:-qiqi-zh}
PITCH=${PITCH:-11}
INDEX_RATE=${INDEX_RATE:-0.5}
INDEX_NPROBE=${INDEX_NPROBE:-16}
API=${API:-http://127.0.0.1:18181}
AUDIO_PORT=${AUDIO_PORT:-18101}
RUN_DIR=${RUN_DIR:-/tmp/mozart-realtime}
# USB 协议对：预处理把采集与播放交给 snd-aloop 桥，主机侧提供麦克风与扬声器。
# 标识与 snd-aloop 的 substream 配对一一对应，两端相同。状态文件由守护进程写入。
BRIDGE_CAPTURE=${BRIDGE_CAPTURE:-plughw:CARD=Loopback,DEV=1,SUBDEV=0}
BRIDGE_PLAYBACK=${BRIDGE_PLAYBACK:-plughw:CARD=Loopback,DEV=0,SUBDEV=1}
BRIDGE_STATUS=${MOZART_STATUS_FILE:-/run/mozart-usb-audio/status.json}
BRIDGE_STALE_SECONDS=${BRIDGE_STALE_SECONDS:-15}

STACK_PID_FILE="$RUN_DIR/stack.pid"
STACK_RUN_FILE="$RUN_DIR/stack-run-dir"
PRE_PID_FILE="$RUN_DIR/pre.pid"
PRE_LOG="$RUN_DIR/pre.log"
PRE_STATUS="$RUN_DIR/pre-status.json"
PRE_DEVICES="$RUN_DIR/pre-devices.json"
READY_TIMEOUT=${READY_TIMEOUT:-90}
STOP_TIMEOUT=${STOP_TIMEOUT:-90}

fail() { echo "错误: $*" >&2; exit 1; }

# 比较设备身份时去掉可选的 SUBDEV：plughw:CARD=Loopback,DEV=1 与 SUBDEV=0 指同一通路。
device_key() { printf '%s' "${1%%,SUBDEV=*}"; }

# 设备存在性检查。-L 清单不含 SUBDEV，比较前剥掉后缀。
require_device() {
    local command=$1 name=$2 device=$3 listing
    listing=$("$command" -L 2>/dev/null) || fail "无法列出 $command 设备"
    [ -n "$listing" ] || fail "无法列出 $command 设备: $command -L 没有输出"
    grep -Fqx -- "$(device_key "$device")" <<<"$listing" \
        || fail "$name不存在: $device（$command -l 查看）"
}

# 输入或输出落在协议对的同一通路上时，桥必须在运行：设备始终可打开，没有桥就是静音。
uses_bridge() {
    [ "$(device_key "$CAPTURE")" = "$(device_key "$BRIDGE_CAPTURE")" ] \
        || [ "$(device_key "$PLAYBACK")" = "$(device_key "$BRIDGE_PLAYBACK")" ]
}

# 桥状态规则由 command_devices 的 bridge 字段定义。这里只报告原因，不复判。
require_bridge() {
    uses_bridge || return 0
    local devices problem
    devices=$(command_devices) || fail "设备清单读取失败"
    problem=$(printf '%s' "$devices" | python3 -c '
import json, sys
bridge = json.load(sys.stdin).get("bridge") or {}
if bridge.get("active") is not True:
    print(bridge.get("reason") or bridge.get("last_error") or "桥未就绪")
')
    [ -z "$problem" ] && return 0
    fail "USB 桥不可用: $problem（sudo systemctl start mozart-usb-audio）"
}

api_request() {
    local method=$1 path=$2 payload=$3
    curl -fsS --max-time "$READY_TIMEOUT" -X "$method" "$API$path" \
        -H 'Content-Type: application/json' -d "$payload" >/dev/null || {
        local code=$?
        echo "后端请求失败: $method $path" >&2
        return "$code"
    }
}

wait_http() {
    local pid=$1 deadline=$((SECONDS + READY_TIMEOUT))
    while (( SECONDS < deadline )); do
        kill -0 "$pid" 2>/dev/null || return 1
        if curl -sf --max-time 2 "$API/api/status" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

wait_stack() {
    local path=$1 pid=$2 phase=$3 deadline=$((SECONDS + READY_TIMEOUT))
    while (( SECONDS < deadline )); do
        if python3 - "$path" "$pid" "$phase" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except FileNotFoundError:
    sys.exit(1)
except json.JSONDecodeError as error:
    print('字幕状态文件损坏: ' + str(error), file=sys.stderr)
    sys.exit(2)
if not isinstance(d, dict) or not all(key in d for key in ('supervisor_pid', 'state', 'phase')):
    print('字幕状态文件缺少必要字段', file=sys.stderr)
    sys.exit(2)
if d.get('supervisor_pid') != int(sys.argv[2]):
    sys.exit(1)
if d['state'] in ('failed', 'stopped', 'completed'):
    print('字幕启动器已退出: ' + str(d.get('error', d['state'])), file=sys.stderr)
    sys.exit(2)
sys.exit(0 if d['phase'] == sys.argv[3] else 1)
PY
        then
            return 0
        else
            local result=$?
            [ "$result" -eq 2 ] && return 1
        fi
        kill -0 "$pid" 2>/dev/null || return 1
        sleep 0.2
    done
    return 1
}

wait_readiness() {
    local path=$1 key=$2 pid=$3 deadline=$((SECONDS + READY_TIMEOUT))
    while (( SECONDS < deadline )); do
        if python3 - "$path" "$key" "$pid" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except (FileNotFoundError, json.JSONDecodeError):
    sys.exit(1)
if d.get('supervisor_pid') != int(sys.argv[3]):
    sys.exit(1)
if d.get('state') in ('failed', 'stopped'):
    print('字幕启动器已退出: ' + str(d.get('error', d.get('state'))), file=sys.stderr)
    sys.exit(2)
sys.exit(0 if (d.get('readiness') or {}).get(sys.argv[2]) else 1)
PY
        then
            return 0
        else
            local result=$?
            [ "$result" -eq 2 ] && return 1
        fi
        kill -0 "$pid" 2>/dev/null || return 1
        sleep 0.2
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
    pid=$(cat "$file") || return
    [[ $pid =~ ^[1-9][0-9]*$ ]] || fail "$file 包含无效 PID"
    if [ -d "/proc/$pid" ]; then
        local started
        started=$(python3 - "$pid" "$3" <<'PY'
import os, signal, sys
from pathlib import Path
pid = int(sys.argv[1])
try:
    fd = os.pidfd_open(pid)
except ProcessLookupError:
    print('gone')
    sys.exit(0)
try:
    command = Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\0')
    if command[0] and sys.argv[2].encode() not in command[:2]:
        sys.exit('PID 所属进程与启动记录不符，停止操作已取消')
    started = Path('/proc/'+str(pid)+'/stat').read_bytes().rsplit(b')', 1)[1].split()[19]
    signal.pidfd_send_signal(fd, signal.SIGTERM)
    print(started.decode('ascii'))
except (FileNotFoundError, ProcessLookupError):
    print('gone')
finally:
    os.close(fd)
PY
) || return
        local deadline=$((SECONDS + STOP_TIMEOUT))
        while (( SECONDS < deadline )) && kill -0 "$pid" 2>/dev/null; do
            [[ $(ps -o stat= -p "$pid") != Z* ]] || break
            sleep 0.2
        done
        if kill -0 "$pid" 2>/dev/null && [[ $(ps -o stat= -p "$pid") != Z* ]]; then
            echo "$name 停止超时，强制回收进程树 (pid $pid)" >&2
            python3 - "$pid" "$started" <<'PY' || return
import os, signal, sys
from pathlib import Path
root = int(sys.argv[1])
processes = {}
for path in Path('/proc').glob('[0-9]*/stat'):
    try:
        fields = path.read_bytes().rsplit(b')', 1)[1].split()
    except FileNotFoundError:
        continue
    processes[int(path.parent.name)] = (int(fields[1]), fields[19])
if root not in processes or processes[root][1] != sys.argv[2].encode():
    print('原进程已退出，跳过强制停止', file=sys.stderr)
    sys.exit(0)
victims = [root]
for pid in victims:
    victims.extend(child for child, (parent, _) in processes.items() if parent == pid)
for pid in reversed(victims):
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        continue
    try:
        fields = Path('/proc/'+str(pid)+'/stat').read_bytes().rsplit(b')', 1)[1].split()
        if fields[19] == processes[pid][1]:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    except (FileNotFoundError, ProcessLookupError):
        pass
    finally:
        os.close(fd)
PY
        fi
        echo "已停止 $name (pid $pid)"
    fi
    mv "$file" "$file.stopped"
}

command_start() {
    [ -x "$BACKEND_BIN" ] || fail "缺少后端二进制 $BACKEND_BIN（先构建 build-gpu）"
    [ -x "$PRE_BIN" ] || fail "缺少预处理二进制 $PRE_BIN（先构建 build/usb-pre）"
    [ -f "$CONFIG" ] || fail "缺少配置 $CONFIG"
    [ -x "$PYTHON" ] || fail "缺少 Python 环境 $PYTHON（设置 PYTHON 或安装 .venv）"
    require_device arecord 采集设备 "$CAPTURE"
    require_device aplay 播放设备 "$PLAYBACK"
    require_bridge
    case "$OUTPUT" in
        voice|speech|both) ;;
        *) fail "OUTPUT 必须是 voice、speech 或 both: $OUTPUT" ;;
    esac
    # 播报服务的检查对 speech 和 both 都适用。
    if [ "$OUTPUT" != voice ]; then
        [ -z "$REFERENCE" ] || [ -f "$REFERENCE" ] || fail "参考音频不存在: $REFERENCE"
        case "$SPEECH_EARLY_AUDIO" in
            0|1) ;;
            *) fail "SPEECH_EARLY_AUDIO 必须是 0 或 1: $SPEECH_EARLY_AUDIO" ;;
        esac
        if [ -n "$TTS_PYTHONPATH" ]; then
            [ -d "$TTS_PYTHONPATH" ] || fail "TTS_PYTHONPATH 目录不存在: $TTS_PYTHONPATH"
            TTS_PYTHONPATH=$(cd -- "$TTS_PYTHONPATH" && pwd)
        fi
    fi
    local api_port
    api_port=$(python3 - "$API" <<'PY'
import sys
from urllib.parse import urlsplit
try:
    url = urlsplit(sys.argv[1])
    port = url.port
except ValueError as error:
    sys.exit('API 地址无效: ' + str(error))
if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.path or url.query or url.fragment or url.username or port is None:
    sys.exit('API 必须为 http://127.0.0.1:端口')
print(port)
PY
)
    mkdir -p "$RUN_DIR"
    command_stop
    local captions_dir=${CAPTIONS_DIR:-$HOME/mozart-archive/logs/realtime-$(date +%Y%m%d-%H%M%S)-$$}
    mkdir -p "$captions_dir/previous-runtime"
    captions_dir=$(cd "$captions_dir" && pwd)
    local file
    for file in pre.log pre-status.json pre-devices.json; do
        [ ! -f "$RUN_DIR/$file" ] || cp -p "$RUN_DIR/$file" "$captions_dir/previous-runtime/"
    done
    printf '%s\n' "$captions_dir" >"$STACK_RUN_FILE"
    # 生效输出设备只在启动时推导一次；pre-restart 用控制面传入的值。
    PRE_PLAYBACK=$PLAYBACK
    if [ "$OUTPUT" = speech ] || { [ "$OUTPUT" = both ] && [ -n "$REFERENCE" ]; }; then
        PRE_PLAYBACK=null
    fi
    local extra=()
    local mode_args=(--mode captions --no-playback)
    if [ "$OUTPUT" != voice ]; then
        mode_args=(--mode full --playback-device "$PLAYBACK")
        [ -z "$REFERENCE" ] || extra+=(--reference "$REFERENCE" --reference-name "$REFERENCE_NAME")
        [ "$SPEECH_EARLY_AUDIO" = 0 ] || extra+=(--early-audio)
        [ -z "$TTS_PYTHONPATH" ] || extra+=(--tts-pythonpath "$TTS_PYTHONPATH")
    fi
    # both：播报未启用时也要开始采集，差异记入 degraded。
    [ "$OUTPUT" != both ] || mode_args+=(--allow-degraded)
    [ -z "$FINAL_MODEL" ] || extra+=(--final-model "$FINAL_MODEL")
    [ -z "${GLOSSARY:-}" ] || extra+=(--glossary "$GLOSSARY")
    [ -z "${CORRECTIONS:-}" ] || extra+=(--corrections "$CORRECTIONS")
    # 任一步骤失败都回收本次启动的进程；字幕启动器负责回收自己的子进程。
    trap 'code=$?; trap - EXIT; if ! RUN_DIR="$RUN_DIR" STOP_TIMEOUT="$STOP_TIMEOUT" "$ROOT/tools/run-realtime-voice.sh" stop; then echo "启动失败后的进程清理失败，请检查 $RUN_DIR" >&2; fi; exit "$code"' EXIT
    SUBTITLE_TARGET=127.0.0.1:18100
    echo "启动后端、字幕和翻译 $MODEL（配置 $CONFIG）"
    setsid "$PYTHON" "$ROOT/tools/run_translated_speech.py" \
        --backend-config "$CONFIG" --api-port "$api_port" \
        --external-capture --archive-utterances --run-dir "$captions_dir" \
        --stt-model "$STT_MODEL" --llama-server "$LLAMA_SERVER" --llama-model "$LLAMA_MODEL" \
        "${mode_args[@]}" "${extra[@]}" >"$captions_dir/supervisor.log" 2>&1 < /dev/null &
    local stack_pid=$!
    echo "$stack_pid" >"$STACK_PID_FILE"
    wait_http "$stack_pid" || { tail -20 "$captions_dir/supervisor.log" >&2; fail "后端在 ${READY_TIMEOUT}s 内未就绪"; }
    wait_stack "$captions_dir/stack-status.json" "$stack_pid" waiting_for_audio \
        || fail "字幕和翻译启动失败，查看 $captions_dir"
    if [ "$OUTPUT" = speech ]; then
        wait_readiness "$captions_dir/stack-status.json" speech_runtime "$stack_pid" \
            || fail "播报运行时未就绪，查看 $captions_dir/supervisor.log"
    elif [ "$OUTPUT" = both ]; then
        # both 允许播报先不可用：这里只警告，链路存活由后面的 capturing 判定。
        wait_readiness "$captions_dir/stack-status.json" speech_runtime "$stack_pid" \
            || echo "警告: 播报运行时未就绪，翻译播报按钮暂不可用，查看 $captions_dir" >&2
    fi

    api_request POST /api/mode/switch '{"mode":"idle"}'
    if [ "$OUTPUT" != speech ]; then
        api_request POST /api/mode/switch "{\"mode\":\"rt_rvc\",\"model_id\":\"$MODEL\"}"
        api_request PUT /api/parameters "{\"pitch_shift\":$PITCH,\"index_rate\":$INDEX_RATE,\"index_nprobe\":$INDEX_NPROBE}"

        local has_index
        has_index=$(curl -sf --max-time 3 "$API/api/status" | python3 -c "import json,sys;print(json.load(sys.stdin)['model'].get('has_index','false'))")
        [ "$has_index" = "true" ] || fail "模型 $MODEL 未加载索引，检查 $CONFIG 与模型目录"
    fi
    # both 且未指定参考音色：预处理占用播放设备，用 stop 关闭会话并杀掉残留播放进程。
    # 播报服务暂不可用时只警告；采集仍应开始。
    if [ "$OUTPUT" = both ] && [ -z "$REFERENCE" ]; then
        curl -fsS --max-time 15 -X POST "$API/api/speech/stop" \
            -H 'Content-Type: application/json' -d '{}' >/dev/null \
            || echo "警告: 未能关闭播报会话，变声仍可试听，查看 $captions_dir" >&2
    fi

    start_pre
    wait_stack "$captions_dir/stack-status.json" "$stack_pid" capturing \
        || fail "字幕未收到有效音频，查看 $captions_dir"
    trap - EXIT

    command_status
    echo
    echo "链路已启动。说话试听；停止用 tools/run-realtime-voice.sh stop"
}

# 用当前 CAPTURE/PRE_PLAYBACK 起预处理，并把生效设备写进 pre-devices.json 供控制面读取。
# PRE_PLAYBACK 是实际输出设备：启动时按 OUTPUT 推导，pre-restart 时由传入参数决定。
start_pre() {
    local pre_playback=$PRE_PLAYBACK
    echo "启动预处理 $CAPTURE -> $AUDIO_PORT -> $pre_playback"
    if [ -f "$PRE_STATUS" ]; then
        mv "$PRE_STATUS" "$PRE_STATUS.previous"
    fi
    local extra=()
    if [ -n "${SUBTITLE_TARGET:-}" ]; then
        extra=(-b "$SUBTITLE_TARGET")
    fi
    setsid "$PRE_BIN" -d "$CAPTURE" -o "$pre_playback" -a 127.0.0.1 -p "$AUDIO_PORT" "${extra[@]}" \
        --mode ai --period-frames 64 --no-denoise \
        --ctl-port 19040 --status-file "$PRE_STATUS" >>"$PRE_LOG" 2>&1 < /dev/null &
    local pre_pid=$!
    echo "$pre_pid" >"$PRE_PID_FILE"
    wait_field "$PRE_STATUS" state ready "$pre_pid" || {
        tail -20 "$PRE_LOG" >&2
        fail "预处理在 ${READY_TIMEOUT}s 内未就绪"
    }
    write_pre_devices
}

write_pre_devices() {
    python3 - "$CAPTURE" "$PRE_PLAYBACK" "${SUBTITLE_TARGET:-}" "$PRE_DEVICES" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[4])
temporary = path.with_suffix('.json.tmp')
temporary.write_text(json.dumps(dict(zip(('capture', 'playback', 'subtitle_target'), sys.argv[1:4]))))
temporary.replace(path)
PY
}

# 列出采集与播放设备。输出一行 JSON，供控制面渲染下拉框。
# 末尾追加 USB 协议对与桥状态；协议对标识只在上文定义一次。
command_devices() {
    python3 - "$BRIDGE_CAPTURE" "$BRIDGE_PLAYBACK" "$BRIDGE_STATUS" "$BRIDGE_STALE_SECONDS" <<'PY'
import json, re, subprocess, sys, time

LINE = re.compile(r'^card (\d+): (\S+) \[(.*?)\], device (\d+): (.*?) \[(.*?)\]')
bridge_capture, bridge_playback, status_file, stale = sys.argv[1:5]
STALE_SECONDS = float(stale)


def collect(command):
    """返回 (可见设备, 全部设备标识)。隐藏的原生 Loopback 仍用于协议对的存在性检查。"""
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f'设备清单读取失败: {" ".join(command)} 返回 {result.returncode}')
    items, present = [], set()
    for line in result.stdout.splitlines():
        match = LINE.match(line)
        if not match:
            continue
        card, short, long_name, device, _, pcm = match.groups()
        identifier = f'plughw:CARD={short},DEV={device}'
        present.add(identifier)
        if short == 'Loopback':
            # 原生 Loopback 设备由 USB 协议对代替；直选会接错 substream。
            continue
        items.append({
            'id': identifier,
            'card': int(card),
            'label': f'{long_name} / {pcm}' if pcm else long_name,
            'group': 'local',
        })
    return items, present


def with_bridge(items, present, bridge_id, label):
    """本机存在对应基础设备时才列出协议项，否则控制面会选中打不开的设备。"""
    if bridge_id.split(',SUBDEV=')[0] not in present:
        return items
    return items + [{'id': bridge_id, 'label': label, 'group': 'usb'}]


def bridge_state():
    try:
        data = json.load(open(status_file))
    except (OSError, json.JSONDecodeError) as error:
        return {'active': False, 'reason': f'读取桥状态失败: {error}'}
    if not isinstance(data, dict):
        return {'active': False, 'reason': '桥状态格式不正确'}
    updated = data.get('updated_at')
    if not isinstance(updated, (int, float)) or time.time() - updated > STALE_SECONDS:
        return {'active': False, 'reason': '桥状态超过刷新窗口',
                'last_error': data.get('last_error', '')}
    state = {'active': data.get('active') is True,
             'role': data.get('role', ''),
             'sent': data.get('sent', 0), 'received': data.get('received', 0),
             'dropped': data.get('dropped', 0), 'gaps': data.get('gaps', 0),
             'errors': data.get('errors', 0),
             'last_error': data.get('last_error', ''),
             'age_seconds': round(time.time() - updated, 1)}
    if not state['active']:
        state['reason'] = state['last_error'] or 'USB 链路未就绪'
    return state


capture, capture_present = collect(['arecord', '-l'])
playback, playback_present = collect(['aplay', '-l'])
# 预处理支持 null 输出；控制面按这份清单校验设备标识。
playback = playback + [{'id': 'null', 'label': '空输出（不播出声）', 'group': 'local'}]

print(json.dumps({
    'capture': with_bridge(capture, capture_present, bridge_capture,
                           'USB 协议 / 管线麦克风（取主机音频）'),
    'playback': with_bridge(playback, playback_present, bridge_playback,
                            'USB 协议 / 管线扬声器（送主机播放）'),
    'bridge': bridge_state(),
}))
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
    require_device arecord 采集设备 "$CAPTURE"
    require_device aplay 播放设备 "$PLAYBACK"
    require_bridge
    mkdir -p "$RUN_DIR"
    stop_pidfile "$PRE_PID_FILE" "预处理" "$PRE_BIN"
    # 控制面传的设备优先：不再按 OUTPUT 覆盖，否则变声/直出按钮静默失效。
    PRE_PLAYBACK=$PLAYBACK
    start_pre
    printf '{"status":"active","capture":"%s","playback":"%s"}\n' "$CAPTURE" "$PRE_PLAYBACK"
}

command_status() {
    local stack_running=0
    if [ -f "$STACK_PID_FILE" ] && kill -0 "$(cat "$STACK_PID_FILE")" 2>/dev/null; then
        stack_running=1
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

    if [ -f "$STACK_RUN_FILE" ]; then
        python3 - "$(cat "$STACK_RUN_FILE")/stack-status.json" "$stack_running" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
if path.exists():
    d = json.loads(path.read_text())
    label = '字幕' if sys.argv[2] == '1' else '字幕历史记录'
    print('%s   state=%s phase=%s translation=%s packets=%s' % (
        label, d['state'], d['phase'], d['readiness']['translation'], d.get('capture', {}).get('packets', 0)))
    if d.get('error'):
        print('       error=%s' % d['error'])
    print('       logs=%s' % path.parent)
PY
    fi
    if [ -f "$PRE_PID_FILE" ] && kill -0 "$(cat "$PRE_PID_FILE")" 2>/dev/null; then
        python3 - "$PRE_STATUS" <<'PY' || echo "预处理  进程在，状态文件缺失"
import json,sys
d=json.load(open(sys.argv[1]))
print('预处理 state=%s mode=%s frames=%s send_errors=%s' % (
    d.get('state'), d.get('mode'), d.get('frames'), d.get('send_errors')))
PY
    else
        echo "预处理  未运行"
    fi
}

command_stop() {
    local result=0
    (stop_pidfile "$PRE_PID_FILE" "预处理" "$PRE_BIN") || result=1
    (stop_pidfile "$STACK_PID_FILE" "后端、字幕与翻译" "$ROOT/tools/run_translated_speech.py") || result=1
    if [ -f "$STACK_RUN_FILE" ]; then
        local directory file
        directory=$(cat "$STACK_RUN_FILE")/runtime-final
        mkdir -p "$directory"
        for file in pre.log pre-status.json pre-devices.json pre.pid.stopped stack.pid.stopped; do
            [ ! -f "$RUN_DIR/$file" ] || cp -p "$RUN_DIR/$file" "$directory/"
        done
    fi
    return "$result"
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
