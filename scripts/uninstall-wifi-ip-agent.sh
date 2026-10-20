#!/bin/bash
# 卸载"回家自动固定 IP"：停掉并删除 LaunchAgent（sudoers 规则需手动删）
set -u
PLIST="$HOME/Library/LaunchAgents/com.hermes-remote.wifiip.plist"
launchctl bootout "gui/$(id -u)/com.hermes-remote.wifiip" 2>/dev/null || true
rm -f "$PLIST"
echo "已移除 LaunchAgent。"
echo "如需一并撤销免密规则：  sudo rm /etc/sudoers.d/hermes-remote-wifi"
