#!/usr/bin/env bash
# 把仓库同步到目标目录、重建、校验并跑冒烟测试。
#
# 用法：
#   tools/deploy.sh --target ~/mozart-demo-20261020 [--rebuild] [--no-smoke]
#
# 同步范围：源码、构建脚本、文档、USB 桥与测试。保留目标的 build-*、temp、state 配置。
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
# 统一用虚拟环境的解释器：子进程继承 sys.executable。
python="${MOZART_PYTHON:-}"
if [ -z "$python" ]; then
    if [ -x "$HOME/Mozart/.venv/bin/python3" ]; then
        python="$HOME/Mozart/.venv/bin/python3"
    else
        python="python3"
    fi
fi
export PATH="$(dirname "$python"):$PATH"
export MOZART_PYTHON="$python"
target=""
rebuild=false
smoke=true

while [ $# -gt 0 ]; do
    case "$1" in
    --target) target="${2:?缺少目录}"; shift 2 ;;
    --rebuild) rebuild=true; shift ;;
    --no-smoke) smoke=false; shift ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

[ -n "$target" ] || { echo "缺少 --target" >&2; exit 2; }
[ -d "$target" ] || { echo "目标目录不存在：$target" >&2; exit 2; }

echo "== 同步源码到 $target"
# tools/ 里没有构建目录，不要用 build* 通配，否则会漏掉 build_pocket_early_audio.py。
rsync -a --delete \
    --exclude='__pycache__' --exclude='*.wav' --exclude='*.mp4' \
    "$root/tools/" "$target/tools/"
rsync -a "$root/preprocessor/src" "$root/preprocessor/include" "$root/preprocessor/tests" \
    "$root/preprocessor/CMakeLists.txt" "$target/preprocessor/"
rsync -a "$root/api/src" "$root/api/include" "$root/api/tests" "$root/api/CMakeLists.txt" "$target/api/"
rsync -a "$root/state/src" "$root/state/include" "$root/state/CMakeLists.txt" "$target/state/"
rsync -a "$root/rvc-backend/src" "$root/rvc-backend/include" "$root/rvc-backend/CMakeLists.txt" \
    "$target/rvc-backend/"
rsync -a "$root/usb-audio/" "$target/usb-audio/" --exclude='__pycache__'
rsync -a "$root/docs/" "$target/docs/"
cp "$root/CMakeLists.txt" "$target/CMakeLists.txt"

echo "== 记录构建信息"
{
    echo "commit=$(git -C "$root" rev-parse --short HEAD)"
    echo "branch=$(git -C "$root" rev-parse --abbrev-ref HEAD)"
    echo "date=$(date -Iseconds)"
} > "$target/BUILD_INFO"

echo "== 校验源码一致"
status=0
while read -r file; do
    relative="${file#$root/}"
    if ! cmp -s "$file" "$target/$relative"; then
        echo "不一致 $relative"
        status=1
    fi
done < <(find "$root/tools" "$root/preprocessor/src" "$root/api/src" "$root/state/src" \
    "$root/rvc-backend/src" "$root/usb-audio" -name '*.py' -o -name '*.c' -o -name '*.cpp' \
    -o -name '*.h' -o -name '*.hpp' -o -name '*.sh' 2>/dev/null | grep -v __pycache__)
[ "$status" -eq 0 ] || { echo "源码不一致，停止" >&2; exit 1; }

if $rebuild; then
    echo "== 重建 C++ 组件"
    cmake --build "$target/build-gpu" -j "$(nproc)" >/dev/null
    echo "完成"
fi

if $smoke; then
    echo "== 冒烟测试"
    "$target/tools/smoke-test.sh"
fi
echo "部署完成：$(cat "$target/BUILD_INFO" | tr '\n' ' ')"
