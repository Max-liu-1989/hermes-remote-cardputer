#!/usr/bin/env bash
# 安装/重装 launchd 自启（macOS）。
#   bash scripts/install_launchd.sh            # 用当前 python 与仓库路径
#   PYTHON=/opt/hermes/.venv/bin/python bash scripts/install_launchd.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$(command -v python3)}"
PLIST_SRC="$REPO/service/com.hermes-remote.bridge.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.hermes-remote.bridge.plist"
LOG_DIR="$HOME/Library/Logs"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "这是 macOS 的 launchd 安装脚本；当前系统不是 macOS。" >&2
  exit 1
fi

"$PYTHON" - <<'PY'
import importlib.util
missing = [m for m in ("PIL", "websockets", "aiohttp") if importlib.util.find_spec(m) is None]
if missing:
    raise SystemExit(f"选定的 Python 缺少依赖 {missing}；换 PYTHON=… 指向 Hermes 的 venv")
PY

mkdir -p "$LOG_DIR" "$(dirname "$PLIST_DST")"
sed -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__REPO__|$REPO|g" \
    -e "s|__HOME__|$HOME|g" \
    "$PLIST_SRC" > "$PLIST_DST"

launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
echo "已安装并启动：com.hermes-remote.bridge"
echo "  日志：$LOG_DIR/hermes-remote.log"
echo "  查看：launchctl list | grep hermes-remote"
echo "  停止：launchctl unload $PLIST_DST"
