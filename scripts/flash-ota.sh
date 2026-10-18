#!/bin/bash
# Wi-Fi 刷机（不需要 USB 线）
#
# 为什么需要：固件用 ARDUINO_USB_CDC_ON_BOOT=1（TinyUSB），USB 只在启动时枚举一次，
# 热插拔之后 Mac 看不到串口（表现为"必须关机再插线才认得到"）。OTA 绕开这个。
#
# 用法：HR_IP=192.168.1.50 ./scripts/flash-ota.sh   # 编译并上传到板子
#       HR_IP 也可写在 ~/.hermes-remote/board.ip 里
set -euo pipefail
cd "$(dirname "$0")/.."

export PATH="$HOME/.local/bin:$PATH"
export HR_TOKEN="${HR_TOKEN:-$(cat "$HOME/.hermes-remote/device.token")}"
HR_IP="${HR_IP:-$(cat "$HOME/.hermes-remote/board.ip" 2>/dev/null || true)}"
if [ -z "$HR_IP" ]; then
  echo "缺少板子 IP：HR_IP=192.168.x.x $0，或写进 ~/.hermes-remote/board.ip" >&2
  exit 2
fi
export HR_IP

cd firmware
pio run -e ota "$@"
pio run -e ota -t upload "$@"
