#!/bin/bash
# 双击运行：在终端里起 Hermes 遥控桥（前台，日志直接打在窗口里）
# 桌面 App 重启换端口也没关系：--rediscover 会自己重找。
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/python ]]; then
  echo "先装一次依赖："
  echo "  cd $(pwd) && uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -e '.[serial]'"
  exit 1
fi

export PYTHONPATH="$(pwd)"
# 转写模型：small 中文够用且快（base 会把 token 听成"周根"）；换模型只影响这一行
export HERMES_REMOTE_STT_MODEL="${HERMES_REMOTE_STT_MODEL:-small}"
exec .venv/bin/python -m hermes_remote.cli -v run --rediscover "$@"
