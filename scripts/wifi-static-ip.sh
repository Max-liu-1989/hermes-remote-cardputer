#!/bin/bash
# 在家的 Linksys Wi-Fi 上用固定 IP（板子配网时钉死 <bridge-ip>），换到别的网络自动回 DHCP。
#
# 为什么需要：Cardputer 里存的是 `HOST <bridge-ip>:47823`，Mac 的 IP 一变板子就显示
# bridge offline。而 macOS 的「私有 Wi-Fi 地址」是随机 MAC，路由器按 MAC 做 DHCP 绑定
# 对不上（实测同一台 Mac 十分钟拿到过 .174 / .175 两个地址）。
#
# 用法：
#   ./wifi-static-ip.sh            # 按当前 SSID 决定（给 launchd 调的）
#   ./wifi-static-ip.sh --dry-run  # 只打印会做什么，不改
#
# 依赖：能把这两条命令免密执行的 sudoers 规则（见 install-wifi-ip-agent.sh）
set -u

IFACE="${HR_WIFI_IFACE:-en0}"
SERVICE="${HR_WIFI_SERVICE:-Wi-Fi}"
STATIC_IP="${HR_STATIC_IP:-<bridge-ip>}"
MASK="${HR_STATIC_MASK:-255.255.255.0}"
GATEWAY="${HR_STATIC_GW:-<gateway-ip>}"
# 家里的 SSID 前缀（不区分大小写）：<home-ssid> / <home-ssid>-5G / <home-ssid>-5 都命中
HOME_PATTERN="${HR_HOME_SSID_PATTERN:-^<home-ssid>}"

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

log() { echo "$(date '+%F %T') $*"; }

ssid=$(networksetup -getairportnetwork "${IFACE}" 2>/dev/null | sed -n 's/^Current Wi-Fi Network: //p')
if [ -z "$ssid" ]; then
  log "Wi-Fi 未连接（${IFACE}），跳过"
  exit 0
fi

home=0
shopt -s nocasematch
[[ "$ssid" =~ $HOME_PATTERN ]] && home=1
shopt -u nocasematch

info=$(networksetup -getinfo "$SERVICE" 2>/dev/null)
mode=$(echo "$info" | head -1)
current_ip=$(echo "$info" | sed -n 's/^IP address: //p')

if [ "$home" = 1 ]; then
  if [ "$mode" = "Manual Configuration" ] && [ "$current_ip" = "$STATIC_IP" ]; then
    log "SSID=$ssid 已是固定 IP $STATIC_IP，无需改动"
    exit 0
  fi
  log "SSID=$ssid 是在家网络 → 设为固定 $STATIC_IP/$MASK gw $GATEWAY"
  [ "$DRY" = 1 ] && exit 0
  sudo -n networksetup -setmanual "$SERVICE" "$STATIC_IP" "$MASK" "$GATEWAY" \
    || log "失败：sudo 需要密码，请先安装 sudoers 规则（install-wifi-ip-agent.sh）"
else
  if [ "$mode" != "DHCP Configuration" ]; then
    log "SSID=$ssid 不是在家网络 → 改回 DHCP"
    [ "$DRY" = 1 ] && exit 0
    sudo -n networksetup -setdhcp "$SERVICE" \
      || log "失败：sudo 需要密码，请先安装 sudoers 规则（install-wifi-ip-agent.sh）"
  else
    log "SSID=$ssid 不是在家网络，已是 DHCP，无需改动"
  fi
fi
