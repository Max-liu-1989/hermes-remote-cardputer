#!/bin/bash
# 安装"回家自动固定 IP"的 LaunchAgent + 免密规则。
#
# 做两件事：
#   1) 写 ~/Library/LaunchAgents/com.hermes-remote.wifiip.plist —— 每 60 秒跑一次脚本
#      （不是 KeepAlive 守护：跑完就退出，不会劫持任何启动流程）
#   2) 打印一条 sudoers 命令给你手动执行 —— 只允许两条 networksetup，别的都不放
#
# 卸载：./uninstall-wifi-ip-agent.sh
set -eu
cd "$(dirname "$0")/.."
REPO=$(pwd)
# launchd 读不了 ~/Documents（TCC 保护，日志会写 Operation not permitted），
# 所以装一份到 Application Support 下，仓库里的这份只做源文件
SRC_SCRIPT="$REPO/scripts/wifi-static-ip.sh"
INSTALL_DIR="$HOME/Library/Application Support/hermes-remote"
SCRIPT="$INSTALL_DIR/wifi-static-ip.sh"
PLIST="$HOME/Library/LaunchAgents/com.hermes-remote.wifiip.plist"
LOG="$HOME/Library/Logs/hermes-remote-wifi.log"
mkdir -p "$INSTALL_DIR" "$(dirname "$LOG")"
cp "$SRC_SCRIPT" "$SCRIPT"
chmod +x "$SCRIPT"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.hermes-remote.wifiip</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$SCRIPT</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/com.hermes-remote.wifiip" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/com.hermes-remote.wifiip"

echo "✅ LaunchAgent 已安装：$PLIST"
echo "   日志：$LOG"
echo
echo "还差一步（需要密码）：允许脚本免密执行这两条 networksetup。"
echo "复制下面整段到终端执行："
echo
cat <<EOF
sudo tee /etc/sudoers.d/hermes-remote-wifi >/dev/null <<'RULES'
$(id -un) ALL=(root) NOPASSWD: /usr/sbin/networksetup -setmanual Wi-Fi <bridge-ip> 255.255.255.0 <gateway-ip>
$(id -un) ALL=(root) NOPASSWD: /usr/sbin/networksetup -setdhcp Wi-Fi
RULES
sudo chmod 440 /etc/sudoers.d/hermes-remote-wifi
EOF
echo
echo "然后手动跑一次验证：  bash $SCRIPT"
