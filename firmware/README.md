# 固件：Cardputer-Adv（ESP32-S3FN8）

## 编译/烧录

```bash
# Mac 上装一次 PlatformIO
brew install platformio          # 或 python3 -m pip install platformio

cd firmware
pio run                          # 编译
pio run -t upload                # 烧录（按住 G0 再插线进下载模式）
pio device monitor               # 串口日志 115200
```

`platformio.ini` 用 `board = esp32-s3-devkitc-1` + `-DM5GFX_BOARD=24`（社区实测：M5GFX 会把 ADV
误判成 StampS3）。若本机装了 `platform-m5stack`，可换成官方 `board = m5stack-cardputer-adv`。

**状态：代码完成，但本仓库的验证环境没有 ESP32 工具链与硬件，`pio run` 没跑过。**
第一次上机请按下面顺序确认，任何一处不对都只影响那一块，不影响桥。

## 第一次上机要确认的五处

1. **能否编译**：`pio run`。库版本见 `platformio.ini`（M5Cardputer ≥1.1.1 才自动识别 ADV 的 TCA8418 键盘）。
2. **屏幕方向/偏移**：`M5Cardputer.Display.setRotation(1)`；若图像倒置或偏移，只改这一行。
3. **键盘**：代码用 `M5Cardputer.Keyboard.keysState()`（`word`/`enter`/`del`/`fn`）与
   `` isKeyPressed('`') `` 等按键判定；反引号键当 ESC。若键位不对，改 `handle_keyboard()` 一处。
4. **侧键 BtnA**：`handle_keyboard()` **先判 BtnA、再判 `Keyboard.isChange()`**（2026-10-18 修；
   之前侧键单独按下永远进不来）。仍要在真机上确认 `M5Cardputer.BtnA` 就是机身侧键，
   按住开始录音、松开进预览。
5. **麦克风**：`M5.Mic.begin()/record()`（M5Unified 对 ES8311 的支持）。录不到音时先打印
   `M5.Mic.record()` 返回值；这里不通只影响语音，屏和按键照常。

帧编解码不需要真机验证：`firmware/include/frame_codec.h` 能在主机上编译，并与 Python 端
用同一份向量交叉比对（`bash scripts/run_all_checks.sh` 的第二步）。

## 配网

```bash
# 串口一次写入 NVS（密码交互式输入，不进 shell history）
python -m hermes_remote.cli provision --port /dev/cu.usbmodem1101 --ssid '家里2.4G'
```

连上桥之后，**token 不再放 URL**：固件在 `WStype_CONNECTED` 时先发
`{"t":"auth","token":"..."}`，桥在认证通过前不推任何帧（10 秒不认证就断开 4401）。
换 token：`python -m hermes_remote.cli rotate-token` → 旧 token 立即失效，板子需重新配网。
`?token=` 的老写法桥仍兼容（只为老固件），新固件不要再用。

写了 `HOST <桥IP> <端口>` 就是**钉死**这个地址：板子不再听 UDP 广播。
这是防同网段假广播的开关（假广播能把板子引到假桥并拿到明文 token），
代价是桥换 IP 要重新配网——所以给 Mac 做 DHCP 保留。

板子开机后有 **8 秒串口窗口**，打印 `READY hermes-remote-provision`，接受：

```
PING
SSID <名字>
PASS <密码>
HOST <桥IP> <端口>      # 可空；空了就靠 UDP 广播发现
TOKEN <32 hex>
COMMIT
```

没有配置时开机会起热点 `HermesRemote-XXXX`，手机连上后在 `192.168.4.1` 的网页里填。

## 板上没有的东西（故意的）

- 没有中文字库、没有排版：屏上内容全部是 Mac 画好的 1bit 位图；
- 没有 Hermes 凭据：只有一个设备 token，桥那边换掉立即失效；
- 没有 SD 卡：WiFi 与 token 存在 NVS；
- 没有“补发队列”：断线期间的按键直接丢弃，恢复后不会把积压的决定送出去。
