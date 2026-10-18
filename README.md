# hermes-remote

把 **M5Stack Cardputer-Adv** 变成 AI agent 的**第二块屏 + 审批键盘**：屏幕上实时显示 agent 在做什么，需要你拍板时直接按键批准/拒绝，按住侧键说话就能给它发消息。

面向局域网（同一 Wi-Fi）：一台 Mac 跑桥接进程，一块 ESP32-S3 小板子当终端。不做广域网远控。

| 状态页 | 审批 | 提问 |
|---|---|---|
| ![status](screens/screen-status.png) | ![approval](screens/screen-approval.png) | ![clarify](screens/screen-clarify.png) |

## 它解决什么

在电脑上跑 agent 时，人不会一直盯着屏幕。审批弹窗出来没人点，任务就卡住；想插一句话又得切回电脑。这块板子解决两件事：

- **看得见**：当前会话在做什么、跑到第几步、最近一条回复，抬眼就有
- **随手批**：审批和提问出现在板子上，按一个键就定，不用找鼠标、不用切窗口

## 架构

```
┌────────────┐   WebSocket（本机）  ┌───────────────┐  WebSocket（局域网）  ┌───────────────┐
│ Hermes     │ ◄──── JSON-RPC ────► │  桥（Python）  │ ◄──── 帧 / 按键 ────► │ Cardputer-Adv │
│ 桌面后端    │                      │  渲染 + 审批   │                       │  只负责显示    │
└────────────┘                      └───────────────┘                       └───────────────┘
```

**画面在 Mac 上渲染**：板子没有中文字库，所有排版、折行、抗锯齿都在 Mac 用 Pillow 画成位图，按 4 位调色板 RLE 压成整帧下发（约 6 KB/帧）。板子只解码贴图，所以换字体、调排版都不用重新烧固件。

## 硬件与软件

- **板子**：M5Stack Cardputer-Adv（ESP32-S3，240×135，全键盘 + 麦克风）。首次刷机需要 USB-C **数据线**。
- **Mac**：Python 3.10+，`websockets` / `aiohttp` / `pillow`；语音可选 `faster-whisper`（Apple Silicon 上自动走 MLX，比 CPU 快约 5 倍）。
- **固件**：PlatformIO + Arduino 框架，依赖 M5Cardputer / M5Unified / WebSockets。

## 快速开始

```bash
git clone https://github.com/<you>/hermes-remote.git
cd hermes-remote
python3 -m venv .venv && . .venv/bin/activate
pip install -e .              # 可选：pip install -e '.[stt,serial]'
```

1) **生成设备 token**（桥与板子共用的共享密钥，别提交进 git）：

```bash
hermes-remote rotate-token        # 写入 ~/.hermes-remote/device.token (0600)
```

2) **给板子配网**（串口，一次就好）：

```bash
hermes-remote provision           # 依次问 SSID / 密码 / 桥地址 / token
```

板子没配置时开机会起热点 `HermesRemote-XXXX`，手机连上后在 `192.168.4.1` 填同样几项也行。

3) **起桥**（前台跑，日志直接可见）：

```bash
hermes-remote run --rediscover
# 或双击 scripts/run-bridge.command
```

桥会自动发现本机 Hermes 桌面后端的端口、attach 当前会话，然后把画面推到板子上。

## 键位

| 页面 | `1`–`4` | `fn` / `del` | `space` | `enter` |
|---|---|---|---|---|
| 状态页 | 切会话 | 翻历史 / 回最新 | 打开最近一条回复 | 回最新 |
| 审批 | 同意一次 / **拒绝** / 本会话 / 永久 | 翻命令全文 | 换下一张待批 | — |
| 提问 | 选对应选项 | 翻正文，翻完翻选项 | 换下一张待批 | — |
| 回复 | — | 翻页 | 换下一条回复 | 收起 |

侧键（BtnA）按住说话、松手转写；开放式提问可以直接用键盘打字作答。

**拒绝键固定为 `2`**，不随后端返回顺序变化——这条是在真机上踩出来的（按 2 想拒绝，结果放行了一整类命令）。

## 安全模型

板子能批准命令，所以这里的取舍比功能更重要：

- 屏幕展示与按键处理共用**同一份校验过的审批信息**，不各自重新解释原始字段
- 确认绑定具体会话、请求与命令内容（`sha1(tool + command)` 摘要）；内容变化或请求切换后旧确认立即失效
- 命令缺失、为空、类型错误，或**无法在设备上完整翻完**时，设备端禁止批准，只允许拒绝
- 高风险操作要求二次确认；二次确认不能弥补信息缺失
- 拒绝永远可用；断线重连**不补发**旧批准
- 后端同时校验请求有效性与处理状态，不只依赖设备界面
- 设备鉴权：连接后第一条消息必须是 token，与 `?token=` 常数时间比对，120 秒内未鉴权即断开

**未做：TLS。** 当前是局域网明文 WebSocket，威胁模型暂定“信任自己的 Wi-Fi”。上公网前必须补传输加密与设备指纹校验。

## 省电

背光闲置 45 秒压到最低，来审批、回复或按键才点亮；Wi-Fi 与心跳常开，所以暗屏时审批照样收得到、响得了。

这块板子**读不到充电状态**（官方文档明确写了 `cannot read battery charging status`，硬读会让固件卡死），所以屏上不会有“充电中”。

## 测试

```bash
python -m unittest discover -s tests -p 'test_*.py'      # 120+ 项
```

覆盖协议编解码、帧对拍（Python 编码 ↔ C++ 解码逐像素比较）、审批键位模型、分页与摘要绑定、断线/过期/补发、语音链路，以及针对真实后端的联调用例（`tests/integration_real_backend.py`，需要本机有 Hermes 后端）。

## 目录

```
hermes_remote/     桥：网关客户端、渲染、审批状态机、设备服务、STT
firmware/          固件（src / include / test）
tests/             Python 测试
scripts/           起桥、OTA 刷机、预览生成、全量检查
docs/              验证记录、整改回执、实施计划、加固记录（含真机踩坑细节）
screens/           屏上样张
```

## 设计取舍

- **画面在 Mac 渲染**，板子只解码：改排版不用烧固件，代价是每帧约 6 KB 带宽
- **4 位调色板**（16 色）而非黑白：颜色分层比加字更省像素；抗锯齿过渡像素硬切纯色，避免小屏糊成一团
- **不用 launchd 常驻**：桥在前台跑，`--rediscover` 每次重新发现后端端口，不给自己装会劫持启动的守护进程
- 真机上踩过的 30 多个缺陷（症状 / 根因 / 修法）见 `docs/加固记录.md`

## License

MIT，见 [LICENSE](LICENSE)。
