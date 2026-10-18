# 协议（桥 ↔ 板子，桥 ↔ 后端）

三份实现必须彼此一致，改任何一边都要跑 `scripts/gen_vectors.py` + 两侧测试：

| 端 | 文件 |
|---|---|
| 桥（Python） | `hermes_remote/protocol.py` |
| 板子（C++） | `firmware/include/frame_codec.h` |
| 测试向量 | `scripts/gen_vectors.py` → `firmware/test/test_frame_codec/vectors.h` |

---

## 1. S 帧：桥 → 板（二进制屏帧）

```
offset  size  字段          说明
0       1     type          0x01 = 屏幕帧
1       2     seq           uint16 BE，回绕；板子丢弃 seq 更旧的帧
3       1     width         240
4       1     height        135
5       1     encoding      0 = RLE1（默认），1 = RAW
6       2     payload_len   uint16 BE
8       1     flags         bit0 = 全屏刷新（板子先清屏）；其余必须为 0
9       n     payload       RLE1 或整帧位图
```

位图：MSB-first、行优先，`240*135/8 = 4050` 字节；屏宽 240 正好 30 字节，**字节永不跨行**。

RLE1：`(count, value)` 字节对，`count ∈ 1..255`，含义是“连续 `count` 个字节都等于 `value`”。
run 跨行连续。典型界面 ≈ 1.0–1.4KB；最坏 8100 字节，仍在 uint16 内。

解码必须做三件校验：`count==0` 非法、总字节超过 4050 非法、不足 4050 也非法（宁可显示
`BAD FRAME`，也不画花屏）。注意 `value` 不是纯色块：`0xAA` 这种字节内部黑白相间，要按位展开。

## 2. K 帧：板 → 桥（JSON 文本）

| 帧 | 例子 | 含义 |
|---|---|---|
| 按键 | `{"t":"key","k":"1"}` | `k` ∈ `1..5` `enter` `esc` `back` `mic` `fn1..3` `left/right/up/down` `confirm` `cancel` |
| 心跳 | `{"t":"hb","rssi":-52,"batt":87,"up":3600,"fw":"1.0.0"}` | 每 15 秒 |
| 上线 | `{"t":"hello","fw":"1.0.0"}` | 桥回 `{"t":"welcome","proto":1}` |
| 录音开始 | `{"t":"voice.begin","rate":16000}` | 桥回 `{"t":"voice.recording"}` |
| 录音结束 | `{"t":"voice.end","ms":3200}` | 桥回 `{"t":"voice.text","text":"…"}` 或 `voice.error` |

未知帧：桥记日志并回一条 `{"t":"error","msg":…}`，**不断开连接**（遥控器不该因为一次笔误掉线）。

## 3. 语音：边录边发（二进制）

板子没有 PSRAM，5 秒 16k/16bit 是 160KB，一次性收完再 POST 很容易 OOM。
所以录音分片走同一条已鉴权的 WS：

```
offset size 说明
0      1    type = 0x02
1      2    len  uint16 BE
3      n    PCM int16 LE @ voice.begin 里的 rate
```

板子每 100ms 发一片（1600 样本 = 3200 字节）；桥在内存里攒到 `voice.end` 再转写，
上限 960KB（约 30 秒）。HTTP 的 `POST /stt` 仍然保留（一次整段上传），供调试/脚本用。

**强制“预览 → 确认”**：转写结果先作为一屏推下去，按 `enter` 才作为 clarify 的自由文本回答；
`esc` 放弃，`mic` 重录。错一个字就照着错的意思干活，这一步不能省。

## 4. 桥 ↔ Hermes 后端（JSON-RPC over WebSocket，只走 127.0.0.1）

| 用途 | 方法 | 关键参数 |
|---|---|---|
| 声明能力 | `client.capabilities` | `{server_requests: true}` —— **不发的话后端根本不会把 clarify/approval 发给你** |
| 列会话 | `session.list` | `{limit}` → `{sessions:[{id,title,…}]}` |
| 挂会话 | `session.activate` | `{session_id, omit_messages:true}` → `LiveSessionSnapshot` |
| 增量 | `session.events.since` | `{session_id, last_seen}` → `{events, latest_seq, open_requests}` |
| 待审批 | `approval.pending` | `{session_id}` → `{approvals:[…]}` |
| 打断 | `session.interrupt` | `{session_id}` |

服务端发起的询问是**服务端请求**（不是通知）：`{"jsonrpc":"2.0","id":"srq-…","method":"clarify","params":{…}}`。
回答就是一条带同一个 id 的响应帧：

```json
{"jsonrpc":"2.0","id":"srq-…","result":{"answers":{"q1":"挂上去"}}}
{"jsonrpc":"2.0","id":"srq-…","result":{"choice":"once"}}
```

- clarify：`params.questions=[{qid,question,choices,multi_select}]`，多题时逐题作答，最后一次性交全；
  没有 `answers` 的响应 = 取消全部。
- approval：`params={request_id,command,tool_name,choices}`，`choices` 一般是 `["once","deny"]`，
  也可能有 `session`/`always`。
- 别的询问（`sudo`/`secret`/`window.read`…）桥明确回 `-32601`，让后端立刻换客户端，而不是等满超时。
- 桌面 App 先答了，后端会发 `request.cancel {id,method,reason}`，桥据此把卡片收掉，**不会再提交一次**。

源码位置（本机 `/opt/hermes`）：

| 结论 | 位置 |
|---|---|
| WS 入口与鉴权 | `hermes_cli/web_routers/chat_ws.py:701`、`hermes_cli/web_server_chat.py::_ws_auth_reason` |
| 多客户端挂同一会话 | `tui_gateway/session_transports.py::_attach_session_transport`（FanoutTransport） |
| 请求/先答者胜/replay | `tui_gateway/server_requests.py`（`send` / `resolve_response` / `open_requests`） |
| 能力声明门槛 | `tui_gateway/server_requests.py::_unanswerable` |
| 快照字段 | `tui_gateway/contracts/sessions.py::LiveSessionSnapshot` |
| 询问契约 | `tui_gateway/contracts/server_requests.py`（clarify / approval） |
