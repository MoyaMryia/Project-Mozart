#!/usr/bin/env bash
# 本地和 CI 共用的检查命令。CI 依赖安装留在 workflow 中。
#   tools/ci-local.sh <lint|python|native|tempo|frontend>  在工作区运行一个阶段
#   tools/ci-local.sh --staged                             提交前检查，pre-commit hook 调用
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

# 阶段名、触发路径（扩展正则，匹配暂存文件路径）。
NATIVE_PATHS='^(rvc-backend|IO|preprocessor|api|state|rvc-golden)/|^CMakeLists\.txt$'
PYTHON_PATHS='^tools/'
TEMPO_PATHS='^preprocessor/|^tools/tempo_player\.py$|^tools/tests/test_tempo_player\.py$'
FRONTEND_PATHS='^frontend/'

stage_lint() { python3 tools/writing_lint.py "$@"; }
stage_python() { python3 -m unittest discover -s tools/tests -v; }
stage_native() {
    cmake -S rvc-backend -B build/host -G Ninja -DCMAKE_BUILD_TYPE=Debug \
        -DUSE_ONNX=OFF -DUSE_TENSORRT=OFF -DBUILD_TESTS=ON -DMOZART_BUILD_TESTS=ON
    cmake --build build/host -j2
    ctest --test-dir build/host --output-on-failure
    ctest --test-dir build/host -R '^state_' --repeat until-fail:10 --output-on-failure
}
stage_tempo() {
    cmake -S preprocessor -B build-gpu/preprocessor -G Ninja \
        -DCMAKE_BUILD_TYPE=Debug -DMOZART_USE_RNNOISE=OFF
    cmake --build build-gpu/preprocessor --target mozart_tempo_filter -j2
    python3 -m unittest discover -s tools/tests -p test_tempo_player.py -v
}
stage_frontend() { npm --prefix frontend test && npm --prefix frontend run build; }

# 测试读取工作区。检查范围内的工作区必须与暂存区一致，测试结果才对应将要提交的内容。
require_clean() {
    local paths="$1" dirty
    dirty=$( { git diff --name-only; git ls-files --others --exclude-standard; } | grep -E "$paths" || true)
    if [ -n "$dirty" ]; then
        echo "以下文件有未暂存或未跟踪的改动。暂存、还原或移走后再提交：" >&2
        echo "$dirty" >&2
        exit 1
    fi
}

staged() {
    local changed base
    changed=$(git diff --cached --name-only)
    [ -n "$changed" ] || exit 0
    base=HEAD
    git rev-parse -q --verify HEAD >/dev/null || base=
    stage_lint --staged ${base:+--base-ref "$base"}
    local name paths
    for name in python native tempo frontend; do
        case $name in
            python) paths=$PYTHON_PATHS ;; native) paths=$NATIVE_PATHS ;;
            tempo) paths=$TEMPO_PATHS ;; frontend) paths=$FRONTEND_PATHS ;;
        esac
        if grep -qE "$paths" <<<"$changed"; then
            require_clean "$paths"
            echo "== $name ==" >&2
            "stage_$name"
        fi
    done
}

case "${1:-}" in
    --staged) staged ;;
    lint) shift; stage_lint "$@" ;;
    python|native|tempo|frontend) "stage_$1" ;;
    *) echo "用法：$0 <lint|python|native|tempo|frontend|--staged>" >&2; exit 2 ;;
esac
