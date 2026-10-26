# hermes-remote · Cardputer-Adv 当 Hermes 的第二块屏 + 远程审批键

把 M5Stack Cardputer-Adv 放在桌上：抬眼能看到 Hermes 正在干什么，它停下来问你要决定时，
按 `1` 放行、按 `2` 拒绝、按住侧键说话当“别的意见”。桌面 App 照常用，遥控器是并行的第二块屏。

这里只有两件事：**全 WiFi**（没有蓝牙、没有 SD 卡、没有中转服务器）和 **屏由 Mac 画**
（板子上一个字体都没有，中文/断行/图标全在 Mac 侧渲染成 1bit 位图推下去）。

**适用场景（边界说清楚）**：人在同一 Wi-Fi 下、但不在桌前。它**不能**从外面远控——
没有 WAN 通路。要“人在外面”，用已有的 Happy/首尔服务器或 Tailscale 暴露后端，不是这块板子。

---

> **2026-10-25 · 兼容 Hermes 桌面端 0.22.x**
> 桌面端从 0.22.x 起不再由 Electron 起 `hermes serve`，改为 launchd 托管的 `gateway run`（端口随机、
> token 不落环境）。桥的发现与协议随之调整：`gateway.sock` 的 `identify` 拿端口、plist 里固定
> `HERMES_DASHBOARD_SESSION_TOKEN`、`session.activate → session.resume`、待答请求改从
> `session.resume` 的 `prompts` 读、作答走 `clarify.respond`/`approval.respond`。
> clarify 卡已真机验证（上屏 → 板上按 1 → 回传 → 确认页）；approval 同一条路径，待真实审批检验。
> 细节与验证范围见 [`docs/2026-10-25-desktop-022.md`](docs/2026-10-25-desktop-022.md)。

## 0. 2026-10-18 加固改了什么（交付版 → 本版）

交付版能跑，但“按下去不一定对”。这一版按“按一次 1 可能放行一条 `rm -rf`”的标准重做了判定，
并在本机（macOS + Hermes 桌面 App 真后端）跑通。逐条对应：

| # | 交付版的问题 | 现在 |
|---|---|---|
| 1 | 危险判定只匹配命令文本，`tool_name` 收下就扔（`DANGEROUS_TOOLS` 是死代码）：`python3 -c 'shutil.rmtree(...)'` 判为不危险 | 两层判定：文本正则 + 工具结构（shell 组合/重定向、内联脚本、写入类动词、`find -delete`、git 写操作）；终端类工具默认可疑，只有明确的只读命令单击放行 |
| 2 | 没有命令原文时判为“安全”（屏上却写着“按 2 拒绝更安全”） | 无原文一律按危险处理 |
| 3 | `_poll_events` 每 3 秒把同一请求重复塞进队列（实测 4 轮后 4 份），答完还会再答 | 按 id 三处去重（当前卡 / 队列 / 最近作答 15 分钟）；取消事件连带清理队列副本 |
| 4 | 断线重连后作答是**假成功**：帧发出去就写“已发送”、写审计 | 先走 `request.answer` RPC，按后端回的 `ok`/`expired` 决定是否算送达；失败时卡片留着、审计写 `delivery_failed`；老后端（`-32601`）才退回响应帧 |
| 5 | `/stt` + `voice.confirm`（`voice:` 前缀）绕过“先预览再发” | 一律先上预览，按 `enter` 才发 |
| 6 | 快照 replay 会把已在 Mac 上答过的请求重新塞回屏上（幽灵卡） | 连续两次快照都看不到当前请求才清卡；replay 不再覆盖正在按的那张 |
| 7 | 默认审计路径没 `expanduser()`：不带 `--audit` 时写到 `./~/.hermes-remote/audit.jsonl` | 修好，并加回归用例 |
| 8 | 桌面 App 的 token 桥拿不到（只注入后端子进程 env，ready file 只有 port） | 新增 `hermes-remote discover`：按 `HERMES_DESKTOP=1` + `HERMES_DASHBOARD_SESSION_TOKEN` 找后端进程，`lsof` 读端口；token 不显示、只写 0600 配置 |
| 9 | 桌面 App 重启后后端端口会变，桥静默失联 | `run --rediscover`：启动和断线时重新发现并改目标 |
| 10 | 交付版拿 `session.list` 的数据库 id 去 `session.activate`，在真桌面后端上必然 `4001 session not found` | 改走 `session.active_list` 拿**活动会话的短 id**（如 `432cd0bc`）再 attach；`session.list` 只作兜底 |
| 11 | 固件：`handle_keyboard()` 先判 `Keyboard.isChange()`，侧键（BtnA）单独按下永远进不来 | BtnA 判定提到最前 |
| 12 | 固件：超时后每 10ms 全屏重绘 `RECONNECTING` | 进入状态时画一次（latch） |
| 13 | 固件：发现包只找子串 `"port":`，同网段可把板子引到假桥并拿到明文 token | 必须含 `"hermes-remote"`；配网写了 `HOST` 就钉死、不再听广播 |
| 14 | `/healthz` 无鉴权、语音 token 永不过期、多设备共用一个录音缓冲 | `/healthz` 无 token 只回“活着”；语音 token 120 秒失效；录音缓冲按连接归属 |

### 第二轮：按《远控 Agent 整改建议》补的硬规则

| # | 整改建议要求 | 现在 |
|---|---|---|
| 15 | 命令缺失/为空/类型错误时**设备端禁止批准**，提示回主端处理 | `ApprovalFacts` 校验：无效请求只能按 2 拒绝，屏上写“不能在设备上批 · 回 Mac 上处理”；`description` 不再顶替 `command` 当命令展示 |
| 16 | 屏幕展示与按键处理共用同一份校验后的审批信息 | 两边都只读 `req.approval`（`ApprovalFacts`：命令、危险判定、理由、分页），渲染不再自己解析 `params` |
| 17 | 长命令必须能完整查阅，否则禁止批准 | 审批页支持翻页（`fn` 下一页 / `back` 上一页），**翻完所有页才允许批准**；超过 8 页（约 540 字符）直接判无效、去主端处理；拒绝随时可用 |
| 18 | 确认绑定具体会话/请求/操作内容，切换或过期后失效 | 二次确认额外绑定 `digest = sha1(tool + 命令原文)`：内容一变、请求切换、**断线重连**都要求重新上膛 |
| 19 | 避免凭据进入访问日志、提供撤销与更新 | 设备 token 不再放 URL：连上后第一条消息 `{"t":"auth","token":…}`，桥收到前不推任何帧（旧固件的 `?token=` 仍兼容）；新增 `rotate-token` 命令 |
| 20 | 分层记录验证，不能混同模拟/联调/编译/硬件 | 新增 `docs/验证记录.md`：四层分开写，本环境没做的标“未验证” |

### 第三轮：拿到真机后暴露的问题（2026-10-18 实机联调）

| # | 真机上才看得到的问题 | 修法 |
|---|---|---|
| 21 | **网关把带 `id` 的 `srq-` 询问丢掉**（`elif method and "id" not in frame`）：clarify/approval 从来到不了桥，偶尔靠轮询兜到 | 收帧分支改成「有 `method` + `srq-` 前缀 → 服务端请求」，推送立即生效 |
| 22 | 广播帧**没有超时**：某个客户端不读，`send_bytes` 一直挂着，把整个渲染循环拖死（frame 计数停住） | 每次发送 3 秒超时，超时就踢掉该客户端 |
| 23 | 板子**从不清 seq**：桥一重启，新帧全被判成“迟到的旧帧”，画面冻结并显示 `RECONNECTING` | 每次 `WStype_CONNECTED` 重置 `g_have_seq/g_last_seq/g_have_fp` |
| 24 | **屏每秒闪一次**：状态行按秒显示运行时长 → 画面签名每秒都变 → 每秒整屏重发 | 时长只显示到分钟；桥只在画面变化时发，每 5 秒补一帧心跳；板子按内容指纹去重，内容相同不重画 |
| 25 | esptool **stub 阶段丢包**（`No serial data received`），第二次烧录就失败 | `platformio.ini` 加 `upload_flags = --no-stub`、`upload_speed = 460800`（ROM 加载器更稳） |
| 26 | **AP 兜底页写不了 token**，保存后仍判无效、无限回到 AP；串口窗口只有开机 8 秒 | 网页表单加 token 字段；未配置时串口**常驻可用**并每 5 秒重发 `READY`；串口配网脚本自动复位+重试 |
| 27 | 桌面 App 的令牌/端口每次重启都变 | `discover` + `run --rediscover`（本轮实机验证：重启桥后板子 2 秒内自动重连） |
| 28 | **审批键位跟着后端顺序走**：`2` 实际是“本会话都同意”，想拒绝却放行整类命令 | 固定键位 `1=同意一次 2=拒绝 3=本会话 4=永久`（`safety.approval_key_map`），渲染与按键共用，3 个回归用例 |
| 29 | **双 socket 重连**：库自带重连 + 自建 `begin()` 并存，服务端以 1002/1006 关闭 | 重连只交给库（5 秒间隔）；加 30 秒无帧看门狗 |
| 30 | ESP32 modem sleep 导致心跳超时误判 | `WiFi.setSleep(false)` + 心跳 20s/10s/3 次；`bridge offline` 只在真离线时画 |
| 31 | **小屏看不清**：实际一直在用 Heiti TC（繁体字面），PingFang 在这台机器上 Pillow 打不开 | 正文 17px **Hiragino Sans GB W6**；抗锯齿灰边硬切到纯色（不再糊） |
| 32 | 审批页**命令被切成半个单词**（`bu`/`ild`）：分页按 26 字符切，17px 下放不下 | 分页降到 22 字符，并加回归用例锁住“分页行必须一次放得下” |
| 33 | 新请求没有声音，人不在桌前就错过 | 桥发 `{t:notify}`，板子响两声；**开机自检音**用来区分“喇叭坏”与“通知没到” |

### 彩色帧（ENC_RLE4）

帧格式从 1 位黑白扩成 **4 位调色板**（每字节两个像素，RLE 不变），16 色表在
`protocol.PALETTE_RGB` 与固件 `frame_codec.h:PALETTE565` 一一对应。

配色：顶栏 正常=白底黑字 / 提问=**橙**底 / 危险=**红**底白字 / 运行=**绿**底；
正文 普通=黑、标签=**蓝**、危险=**红**、成功=**绿**、提示=深灰；底栏灰底黑字。

跨语言对拍（`firmware/test/test_frame_codec/xcheck.cpp`）：桥编码 → C++ 解码，
4 个页面 × 32400 像素**逐像素一致**。帧变大到 5–7 KB（原 1.7 KB），局域网无压力。

**真机验证结果**（2026-10-18）：

- 编译：`Flash 33.6% / RAM 17.6%`（与交付记录 33.7% 对齐）
- 烧录：`Wrote 1125008 bytes … Hash of data verified.`
- 屏幕：状态页实时显示当前会话；不再反复闪
- 按键：**板子上按 1 → 后端收到答案**（审计 `request.answered via=device delivery=ok`，`age_s=11.7`）
- 断连自愈：桥重启后板子自动重连（seq 修复后画面立即恢复）

**验证**（本机 macOS 26.6.2，2026-10-18）：

- `bash scripts/run_all_checks.sh` → **全部通过**：**82** 个测试（交付版 43 + 第一轮 20 + 第二轮 19）、C++ 帧向量、固件语法检查、四屏导出
- 第一轮 20 条回归在**未改动的交付版**上跑：**16 条失败** —— 它们真的在拦回归
- 第二轮三项行为，同一探针跑交付版 vs 现在：
  `缺 command 按 1` → 交付版放行 / 现在“内容不完整，只能拒绝”；
  `3 页长命令连按两次` → 交付版放行 / 现在“还有 2 页没看”；
  `上膛→断线→再按 1` → 交付版仍放行 / 现在要求重新上膛
- 本机真后端实跑：`discover` → `/api/ws` token 通过 → `session.active_list` → `attach 432cd0bc status=working`
  → 设备用首条 auth 消息鉴权通过 → 收到 1716B RLE 屏帧（`docs/live-check-2026-10-18.png`）

---

## 1. 现在到哪一步了（照实说）

| 部分 | 状态 | 怎么验的 |
|---|---|---|
| Mac 侧桥（发现后端、挂会话、压状态、画屏、按键路由、语音、重连、审计、自动发现） | **完成** | 82 个自动化测试 + 本机真后端实跑 |
| 桥挂真实桌面 App 后端 | **本机验证通过** | 自动发现 token/端口、`/api/ws`、`active_list`、`attach`、真屏帧 |
| 帧编解码（Python ↔ C++） | **两侧一致** | 同一份向量交叉验证：Python 编码 → C++ 解码逐字节比对（本机 g++ 编译运行通过） |
| 固件（WiFi、WS、画屏、键盘、录音上行、串口配网、AP 兜底、心跳、重连） | **交付版已编译通过；本次改动未重编译、未烧录** | 交付版在迷你主机上用 PlatformIO 6.2.0 + `xtensa-esp32s3-elf-gcc 8.4.0` 编译成功（Flash 33.7%，1.19MB 镜像，见 UM88-22）；本机无 ESP32 工具链，本次 5 处固件改动需要重新 `pio run` |
| 真机麦克风（ES8311 + M5Unified `M5.Mic`） | **未验证** | 需真机；不通只影响语音，屏和按键照常 |
| launchd 自启 | **不建议装** | 见 §6 第 1 条 |

数字对得上：一屏 240×135 单色 = 4050 字节裸位图，典型界面 RLE 后 **≈1.2–1.8KB**
（实测 1270–1736 字节，本机真会话那一帧 1716 字节），1Hz 推给 2.4G 是零负担。

## 2. Mac 上的安装（3 步）

```bash
cd hermes-remote
PY=/path/to/python3            # 需要 pillow / websockets / aiohttp
$PY -m pip install -e .
```

**① 让桥认识桌面 App 的后端。** 桌面 App 把 token 只注入后端子进程的环境变量，
ready file 只有端口。所以：

```bash
$PY -m hermes_remote.cli discover          # 写 ~/.hermes-remote/config.json（0600），token 不回显
$PY -m hermes_remote.cli discover --print  # 只看找没找到
```

找不到（桌面 App 没开）时的退路：自己起一个固定 token 的后端
`HERMES_DASHBOARD_SESSION_TOKEN=xxx hermes dashboard`，再 `run --url ... --token xxx`。
注意那样挂的是**另一个后端**，桌面 App 里正在跑的会话不在里面。

**② 自检。**

```bash
$PY -m hermes_remote.cli doctor
```

它会逐项告诉你：中文字体在不在、端口能不能绑、语音模型能不能加载、后端连不连得上、Mac 睡眠设置怎么样。

**③ 前台常驻（不要装 launchd）。**

```bash
$PY -m hermes_remote.cli run --rediscover
```

`--rediscover` 在启动和每次断线时重找桌面后端：桌面 App 重启后端口变了也能自己接上。

## 3. 板子

```bash
cd firmware
pio run                 # 编译（需要 PlatformIO；本仓库没在真机上编译过）
pio run -t upload       # USB 烧录
```

配网（密码不再走命令行参数）：

```bash
# A. 串口写一次 NVS（推荐）：密码交互式输入，不进 shell history
$PY -m hermes_remote.cli provision --port /dev/cu.usbmodem1101 --ssid '家里2.4G'
#    也可以 --password-stdin 从管道读

# B. 兜底：没配过的板子开机会起热点 HermesRemote-XXXX，手机连上后在 192.168.4.1 的网页里填
```

板子不记 IP：桥每 5 秒在 UDP 47824 广播 `{"t":"hermes-remote","v":1,"port":47823,...}`，
板子只认带 `"hermes-remote"` 的包；广播里没有 token。
**device token 不再放 URL**：连上 `/ws` 后第一条消息发 `{"t":"auth","token":...}`，桥在认证通过前
不推任何帧（10 秒不认证就断开）。token 不进访问日志、不进浏览器历史；换 token 用
`hermes_remote.cli rotate-token`，旧 token 立即失效、板子需重新配网。
**配网时写了 `HOST <桥IP> <端口>` 就钉死这个地址、不再听广播** —— 这是防同网段假广播的开关，
桥的 IP 变了要重新配网（或给 Mac 做 DHCP 保留地址）。

## 4. 屏上长什么样

`$PY -m hermes_remote.cli frames --out ./screens` 会导出四种屏（仓库 `screens/` 已生成）：

| 页面 | 你看到什么 | 能按什么 |
|---|---|---|
| 状态 | 跑着/等你决定 · 已跑 4m12s；正在哪个工具、最后一行输出 | `1-3` 切会话，`esc` 连按两次打断 |
| 问题（clarify） | 第 i/n 题 + 问题原文 + 带序号的选项 | `1-4` 选，按住侧键说话 |
| 审批（approval） | **会话名 + 工具名 + 命令原文**（截断保留动词和目标） | `1-4`；危险命令要按两次 |
| 语音预览 | 转写出来的整句话 | `enter` 发出，`mic` 重录，`esc` 放弃 |

安全上不省的几件事：

- **命令内容不完整就批不了**：`command` 缺失、为空、类型不对，或者长得超过屏上可完整查阅的范围
  （8 页、约 540 字符），设备端**只能拒绝**，提示回 Mac 上处理；`description` 不会顶替命令上屏；
- **屏上与按键读同一份判定**（`ApprovalFacts`）：危险与否、要不要按两次、能不能批，都由它一次算出，
  渲染不再自己解释 `params`；
- **长命令必须翻完**：审批页 `fn` 下一页、`back` 上一页，翻完所有页才接受批准；拒绝随时可用；
- **危险判定两层**：命令原文的正则（`rm -rf`、强推、`sudo`、`curl|sh`、`DROP TABLE`…）+ 工具层结构判定
  （shell 组合/重定向、`python -c` 这类内联脚本、写入类动词、`find -delete`、git 写操作）；
  终端类工具的**未知命令默认按两次**，只有 `ls`/`cat`/`grep`/`git status`/`pytest` 这类只读命令单击放行；
- **二次确认绑定内容**：按一次只是上膛，第二次必须是对同一请求的同一选择；命令内容变化、请求切换、
  断线重连都会让上膛作废；
- 屏上永远先写“这是哪个会话、哪个工具、什么命令”，避免家里开着几个会话时按错；
- 超过 10 分钟的旧请求不接受按键（防断线恢复后“补发”）；
- 每次远程作答留一行审计 `~/.hermes-remote/audit.jsonl`（0600）：成功 `request.answered`（带 `delivery`），
  没送到 `delivery_failed`，过期 `request.expired`；审计里不含 token；
- 板子不存任何 Hermes 凭据，只有一个设备 token；`rotate-token` 一换，旧的一律失效。

## 5. 验收（用你的真实动作，不看日志）

| 动作 | 预期 |
|---|---|
| 让 Hermes 干一件要 3 次审批的活，人走开 | 屏上 1 秒内出现“等你决定”，按 1 放行三次，活干完，全程没碰 Mac |
| 在 Mac 上先答同一个问题 | 板子的卡片 1 秒内消失，不会重复提交 |
| 拔掉 2.4G 十秒再插回 | 板子显示 `RECONNECTING`，恢复后画面与状态一致 |
| 按住侧键说一句话 | 先出预览，`enter` 才发；`esc` 放弃；`mic` 重录 |
| 断网期间乱按 | 恢复后不会把积压的按键补发出去 |
| 重启桌面 App | 桥自动重新发现端口并挂上，无需手动干预 |
| 真机上按 `fn` / `back` 翻审批页 | 翻完最后一页前，按 1 不放行；屏上提示“还有 N 页没看” |
| 发一条没有 `command` 的审批 | 屏上写“不能在设备上批”，按 1 无效，按 2 拒绝有效 |

前两条已经在 `tests/integration_real_backend.py` 里用真实后端跑过；第 2 条另有专门的端到端用例
（`test_card_disappears_when_mac_answers_first`）。

## 6. 已知边界（不粉饰）

1. **不要装 launchd。** `service/com.hermes-remote.bridge.plist` 用的是 `RunAtLoad` +
   `KeepAlive{SuccessfulExit:false, NetworkState:true}`，正是本机 2026-09-22 事故里那种常驻守护形态；
   而且 `caffeinate -dims` 会把 Mac 的睡眠**永久**顶住。要常驻就在终端里跑 `run`。
2. **合盖就睡，任务本身也停。** 桥用 `caffeinate` 顶住系统睡眠，但合盖这条绕不过去：
   要么开盖，要么接电+外显（clamshell）。
3. **固件基线编译过、真机没上过。** 交付版在迷你主机上编译成功（`xtensa-esp32s3-elf-gcc 8.4.0`，Flash 33.7%）；
   **本次 5 处改动没有重编译**（本机无工具链）。第一次上机要确认：能否编译、屏幕方向、
   TCA8418 键盘回调、`BtnA` 是否侧键、ES8311 麦克风（见 `firmware/README.md`）。
4. **语音识别错字。** 型号、人名、代码名几乎必错，所以强制“预览→确认”，不做说完即发。
5. **只支持 2.4G**，且需与 Mac 同网段；访客网络隔离会直接连不上（`doctor` 里能看出来）。
6. **一次只盯一个会话**（`1-3` 切换），不做多会话看板、不做历史回放。
7. **桌面 App 的后端只让 attach “活动会话”**：它当前没有活着的会话时，屏上写“还没有会话”，
   等 Hermes 开始干活就自动挂上。
8. **局域网内没有 TLS（未做，不是忘了）**：device token 已从 URL 挪到首条 auth 消息，
   但连接本身仍是明文 WS，同网段嗅探能看到屏帧内容。威胁模型目前限于“信任自己家的 2.4G”；
   要做到“加密且验证对端身份”，需要桥上 TLS + 板子内置证书/指纹校验，这一条**没有硬件时无法验证**，
   列为下一步。
9. **审批页正文只有两行**：长命令要翻页；翻完才允许批。超过 8 页的命令请回 Mac 处理。

## 7. 目录

```
hermes_remote/
  protocol.py      帧编解码、K 帧、语音分片（与固件逐字节对齐）
  gateway.py       后端 JSON-RPC over WS（capabilities / active_list / attach / 事件 / 询问）
  state.py         状态 → 四行屏；clarify 多题；approval 的危险判定输入
  renderer.py      PIL 画 240×135 1bit + 断行/省略号 + RLE
  device_server.py 板子侧 WS + /stt + /healthz
  bridge.py        按键路由、作答（RPC 优先）、审计、1Hz 心跳、重连、队列去重
  safety.py        统一审批模型（ApprovalFacts：校验/危险判定/分页）、答案形状
  audit.py         JSONL 审计（0600，不含凭据）
  stt.py           faster-whisper（本地，音频不出网）+ 假实现
  power.py         caffeinate 睡眠抑制
  beacon.py        UDP 47824 发现（含子网定向广播）
  desktop_discovery.py  在本机找到桌面 App 的后端（ps/lsof，token 不打印）
  config.py        配置/发现/token 文件
  cli.py           run / discover / rotate-token / doctor / demo / frames / provision / audit
firmware/          PlatformIO 工程 + 主机可测的 frame_codec.h
tests/             单元 / 端到端 / 加固回归 / 审批模型 / 真实后端集成
scripts/           gen_vectors.py、run_all_checks.sh、install_launchd.sh（不建议跑）
service/           launchd plist 模板（不建议装）
docs/              协议说明、正式实施计划、验证记录、本机实测截图
```

跑全部验证：

```bash
PY=/path/to/python3 bash scripts/run_all_checks.sh
$PY tests/integration_real_backend.py   # 需要本机有 Hermes 源码
```
