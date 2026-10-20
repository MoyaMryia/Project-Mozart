#!/usr/bin/env bash
# 把本仓库的 git hook 目录设为 .githooks。只修改本仓库的 git 配置。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
chmod +x .githooks/pre-commit tools/ci-local.sh
git config core.hooksPath .githooks
echo "core.hooksPath = $(git config --get core.hooksPath)"
