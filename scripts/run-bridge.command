#!/bin/bash
# Hermes 遥控终端桥（局域网设备端口 47823）
#
# 后端：独立 `hermes serve` on 127.0.0.1:9119。
# 为什么不用 --rediscover：桌面 App 0.22.x 改成内嵌 gateway（随机端口 + 内存 token），
# 外部进程读不到凭证；改为本机固定 9119 + 固定 token，桌面 App 重启后会 attach 同一个后端。
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"

TOKEN_FILE="$HOME/.hermes-remote/backend.token"
if [ ! -s "$TOKEN_FILE" ]; then
  echo "缺少后端 token：$TOKEN_FILE"
  echo "起后端：HERMES_DASHBOARD_SESSION_TOKEN=\$(cat $TOKEN_FILE) hermes serve --port 9119"
  exit 1
fi

exec .venv/bin/python -m hermes_remote.cli -v run \
  --rediscover \
  --no-beacon --no-keep-awake
