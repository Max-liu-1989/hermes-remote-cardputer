"""桥的内存状态与“屏上到底写什么”。

所有中文、断行、省略号都在这里决定，板子只负责画位图。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

STATUS_LABEL = {
    "idle": "待命",
    "starting": "启动中",
    "waiting": "等你决定",
    "working": "跑着",
    "streaming": "输出中",
    "resuming": "恢复中",
}

PAGE_STATUS = "status"
PAGE_PROMPT = "prompt"
PAGE_VOICE = "voice"
PAGE_OFFLINE = "offline"
PAGE_CONFIRM = "confirm"
PAGE_REPLY = "reply"
PAGE_INPUT = "input"

#: 超过这个秒数的请求不再接受按键（防止断线恢复后“补发”）
STALE_AFTER_S = 600.0

#: 危险操作第二次确认的有效期
CONFIRM_WINDOW_S = 20.0


@dataclass
class SessionRow:
    id: str
    title: str = ""
    status: str = ""
    running: bool = False
    turn_started_at: float | None = None

    @property
    def short_title(self) -> str:
        return (self.title or self.id)[:28]


@dataclass
class PendingRequest:
    """一个还没答的 server→client 请求（clarify / approval）。"""

    id: str
    method: str
    params: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    session_id: str = ""
    #: clarify 多题时逐题作答
    q_index: int = 0
    answers: dict[str, str | None] = field(default_factory=dict)
    #: 危险操作的二次确认（绑定到具体请求内容：digest 变了就不算数）
    confirm_armed: bool = False
    confirm_at: float = 0.0
    confirm_choice: str | None = None
    confirm_digest: str = ""
    #: 审批页翻页（长命令必须翻完才允许批）
    page: int = 0
    seen_pages: set[int] = field(default_factory=set)

    @property
    def approval(self):
        """这次审批的唯一事实来源；屏上与按键都只读它。"""
        from .safety import approval_facts

        return approval_facts(self.params)

    @property
    def age(self) -> float:
        return time.time() - self.created_at

    @property
    def is_stale(self) -> bool:
        return self.age > STALE_AFTER_S

    # ── clarify ──────────────────────────────────────────────────────────────
    @property
    def questions(self) -> list[dict]:
        qs = self.params.get("questions")
        return qs if isinstance(qs, list) else []

    @property
    def question(self) -> dict:
        qs = self.questions
        if not qs:
            return {}
        return qs[min(self.q_index, len(qs) - 1)]

    # ── approval ─────────────────────────────────────────────────────────────
    @property
    def choices(self) -> list[str]:
        if self.method == "clarify":
            q = self.question
            cs = q.get("choices") if isinstance(q, dict) else None
            return [str(c) for c in cs] if isinstance(cs, list) else []
        cs = self.params.get("choices")
        if isinstance(cs, list) and cs:
            return [str(c) for c in cs]
        return ["once", "deny"]


@dataclass
class ViewLine:
    text: str
    style: str = "body"  # body | label | danger | ok | dim
    wrap: bool = True    # False = 一行放不下就省略号（状态页这种摘要不要折成两行浪费版面）


def _is_bot_session(row: dict) -> bool:
    """群聊/bot 会话：板子不该挂上去，也不该占会话位。

    它们在 active_list 里常常一直是 working（bot 在自说自话），
    按“跟正在跑的会话”会被它们劫持，人就切不回自己的对话了。
    """
    preview = str(row.get("preview") or "")
    title = str(row.get("title") or "")
    low = title.lower()
    if preview.lstrip().startswith("[Group chat:"):
        return True
    if "group chat" in low or "bot chat" in low or low.startswith("bot "):
        return True
    if low.startswith("group") or "group:" in low:
        return True                       # "Group: rmvdm" 这类群聊会话
    if "you are @" in preview[:200].lower():
        return True                       # bot 人设提示词
    # bot 会话常见的 "@persona" 署名
    return bool(title.startswith("@") or preview[:40].count("(@"))


def _short_target(key: str, value: str) -> str:
    """屏上只要“在看哪儿 / 在跑啥”，不要整条 shell 命令。

    `cd ~/Documents/hermes-remote && sed -n '...' state.py` → `sed -n '...' state.py`
    （命令原文留在审批页，那里有大字全文和翻页；状态页只做索引）。
    """
    v = " ".join(value.split())
    if key == "command":
        while v.startswith("cd ") and " && " in v:
            v = v.split(" && ", 1)[1]
        if " && " in v:
            v = v.rsplit(" && ", 1)[-1]
        return v[:20]                     # 剩下的交给渲染器加省略号，别在这里硬切到看不出结尾
    if key in ("path", "file_path"):
        parts = [p for p in v.split("/") if p]
        return (parts[-1] if parts else v)[:18]   # 只要文件名，路径在状态页没意义
    return v[:20]


@dataclass
class View:
    page: str
    title: str = ""
    lines: list[ViewLine] = field(default_factory=list)
    footer: str = ""
    #: 顶部条是否反色（等你决定 / 危险）
    bar: str = "normal"  # normal | alert | danger | ok
    #: 屏上显示的键位提示，测试直接断言
    hint: str = ""
    #: 紧凑排版：15px 字、行高 18、顶栏 20、底栏 19 → 正文能放 5 行（状态页用）
    dense: bool = False


class BridgeState:
    """桥的全部可见状态。线程/协程内单写者，读屏时只读。"""

    def __init__(self) -> None:
        self.sessions: list[SessionRow] = []
        self.active_index: int = 0
        self.pending: PendingRequest | None = None
        #: 提问页选项翻页（一页 4 个；超过 4 个用 fn/del 翻）
        self.choice_page: int = 0
        #: 键盘打字作答（开放式提问没有选项时的后路）
        self.input_text: str | None = None
        #: 语音转写确认页的分页（长句一屏放不下；以前只画一行，后面全被截掉）
        self.voice_pages: list[list[str]] = []
        self.voice_page: int = 0
        #: 提问正文分页（问题太长时 fn/del 翻；每页最多 2 行）
        self.q_pages: list[list[str]] = []
        self.q_page: int = 0
        #: 选项分页（每页最多 2 行；fn 翻完正文后接着翻选项）
        self.opt_pages: list[list[str]] = []
        self.opt_page: int = 0
        self.voice_preview: str | None = None
        self.voice_token: str | None = None
        #: 录音/转写阶段：板子上必须看得见，否则按住键半天没动静＝「坏了？」
        self.recording: bool = False
        self.transcribing: bool = False
        self.connected_backend = False
        self.device_connected = False
        self.device_rssi: int | None = None
        self.device_battery: int | None = None
        #: 0 放电 / 1 充电中 / 2 未知（板子心跳里的 chg）
        self.device_charging: int | None = None
        self.last_tool: str | None = None
        self.last_output: str = ""
        #: 最近的工具事件（工具 + 结果成对），状态页滚动显示最近两条。
        #: 只留"前因后果"，不留命令原文——原文属于审批页，那里有大字全文。
        self.recent: list[dict] = []
        #: 最近一次助手回复（message.complete）的摘要：板子上要能看到"它说了什么"
        self.last_reply: str = ""
        #: 状态页正文滚动：0 = 最新；>0 = 往前看了几页（fn 往前、del 回最新）
        self.scroll: int = 0
        self.max_scroll: int = 0
        #: 助手回复：成段显示、可翻页（以前只塞一行 60 字，等于没有）
        self.reply_text: str = ""
        self.reply_pages: list[list[str]] = []
        self.reply_page: int = 0
        self.reply_at: float = 0.0
        #: 回复队列：一轮里可能连发多条，前几条也要能翻回去（space 换）
        self.reply_q: list[dict] = []
        self.reply_qi: int = 0
        self.reply_ttl: float = 180.0
        #: 待处理请求数（当前这张卡 + 队列），由桥每帧填
        self.queue_len: int = 0
        self.status_note: str = ""
        self.last_key_result: str = ""
        self.last_key_at: float = 0.0
        #: 按键之后的结果页（"已拒绝 ✓"）：板子上按完必须立刻有反馈，
        #: 否则人不知道自己到底批了没有——这是最要命的歧义。
        self.confirm_text: str = ""
        self.confirm_detail: str = ""
        self.confirm_style: str = "ok"      # ok | danger
        self.confirm_until: float = 0.0
        self.snapshot: dict = {}

    # ── 会话 ────────────────────────────────────────────────────────────────
    @property
    def active(self) -> SessionRow | None:
        if not self.sessions:
            return None
        return self.sessions[min(self.active_index, len(self.sessions) - 1)]

    def set_sessions(self, rows: list[SessionRow]) -> None:
        self.sessions = rows
        if self.active_index >= len(rows):
            self.active_index = 0

    def cycle_session(self, delta: int = 1) -> None:
        if not self.sessions:
            return
        self.active_index = (self.active_index + delta) % len(self.sessions)

    # ── 快照 ────────────────────────────────────────────────────────────────
    def apply_snapshot(self, snap: dict) -> None:
        """把 session.activate / session.events.since 的 LiveSessionSnapshot 吃进来。"""
        self.snapshot = snap or {}
        sid = snap.get("session_id") or ""
        title = ""
        info = snap.get("info") or {}
        if isinstance(info, dict):
            title = info.get("title") or info.get("name") or ""
        row = SessionRow(
            id=sid,
            title=title,
            status=str(snap.get("status") or ""),
            running=bool(snap.get("running")),
            turn_started_at=snap.get("turn_started_at"),
        )
        replaced = False
        for i, existing in enumerate(self.sessions):
            if existing.id == sid:
                self.sessions[i] = row
                self.active_index = i
                replaced = True
                break
        if not replaced and sid:
            self.sessions.insert(0, row)
            self.active_index = 0

        if snap.get("open_requests"):
            self.restore_requests(snap["open_requests"])
        if snap.get("pending_approval"):
            pa = snap["pending_approval"]
            rid = str(pa.get("request_id") or "")
            if rid and (self.pending is None or self.pending.id != rid):
                self.pending = PendingRequest(id=rid, method="approval", params=pa,
                                              session_id=sid)

    def restore_requests(self, open_requests: list[dict]) -> None:
        """晚连上也不能漏掉已经弹出的问题：replay 里的 open_requests 里挑一个当当前卡。

        已经有一张卡在屏上时**不覆盖**它（否则正在按的那张会被 replay 换掉，
        用户按的键落到另一条请求上）。其余的交给 Bridge 的队列（_absorb_open_requests）。
        """
        if self.pending is not None:
            return
        for entry in open_requests:
            rid = entry.get("id")
            method = entry.get("method")
            params = entry.get("params") or {}
            if not rid or method not in ("clarify", "approval"):
                continue
            self.pending = PendingRequest(id=str(rid), method=str(method), params=params,
                                          session_id=str(params.get("session_id") or ""),
                                          created_at=float(entry.get("created_at") or time.time()))
            return

    # ── 事件 ────────────────────────────────────────────────────────────────
    def apply_event(self, etype: str, payload: dict) -> None:
        if etype == "tool.start":
            name = payload.get("name") or "工具"
            args = payload.get("args") or {}
            self.last_tool = str(name)
            target = ""
            if isinstance(args, dict):
                for key in ("path", "file_path", "command", "query", "url"):
                    if args.get(key):
                        target = _short_target(key, str(args[key]))
                        break
            self.status_note = f"{str(name)[:7]} {target}".strip()[:30]
            self.recent.append({"tool": str(name), "target": target, "summary": ""})
            del self.recent[:-20]          # 留 20 条历史，屏上 5 行一页，fn/del 翻
        elif etype == "tool.complete":
            summary = payload.get("summary") or payload.get("result_text") or ""
            if summary:
                self.last_output = " ".join(str(summary).split())[:80]
                for entry in reversed(self.recent):
                    if not entry["summary"]:
                        entry["summary"] = self.last_output[:44]
                        break
            self.status_note = ""
        elif etype == "status.update":
            self.status_note = str(payload.get("text") or "")[:60]
        elif etype == "message.complete":
            text = payload.get("text") or payload.get("content") or ""
            if text:
                flat = " ".join(str(text).split())
                self.last_output = flat[-80:]
                self.last_reply = flat[:60]
            self.status_note = ""
        elif etype == "request.cancel":
            rid = str(payload.get("id") or "")
            if self.pending and self.pending.id == rid:
                self.pending = None
                self.note("已在 Mac 上处理")
        elif etype == "approval.cancelled":
            self.pending = None
            self.note("审批已撤回")

    def set_reply(self, text: str, pages: list[list[str]] | None = None) -> None:
        """新回复进队列（不是覆盖）：一轮里助手可能连发好几条，前几条也得能翻回去。"""
        if self.reply_q and self.reply_q[-1]["text"] == text:
            self.reply_qi = len(self.reply_q) - 1        # 同一条重报（重连补发）不重复入队
        else:
            self.reply_q.append({"text": text, "pages": pages or [[text]], "at": time.time()})
            del self.reply_q[:-8]                        # 最多留 8 条
            self.reply_qi = len(self.reply_q) - 1
        self._apply_reply()

    def _apply_reply(self) -> None:
        if not self.reply_q:
            return
        item = self.reply_q[self.reply_qi]
        self.reply_text = item["text"]
        self.reply_pages = item["pages"]
        self.reply_page = 0
        self.reply_at = time.time()

    def cycle_reply(self, step: int = 1) -> bool:
        """在队列里换一条回复（space）。返回是否真的换了。"""
        if len(self.reply_q) <= 1:
            return False
        self.reply_qi = (self.reply_qi + step) % len(self.reply_q)
        self._apply_reply()
        return True

    def dismiss_reply(self) -> None:
        self.reply_at = 0.0

    def scroll_by(self, delta: int) -> bool:
        """翻状态页正文：delta=+1 往前（更早），-1 往后（更新）。返回是否真的动了。"""
        new = max(0, min(self.max_scroll, self.scroll + delta))
        if new == self.scroll:
            return False
        self.scroll = new
        return True

    def note(self, text: str) -> None:
        self.last_key_result = text
        self.last_key_at = time.time()

    def confirm(self, text: str, detail: str = "", *, style: str = "ok",
                seconds: float = 3.0) -> None:
        """按键已生效：在屏上明确显示结果，避免“按了但不知道成没成”。"""
        self.note(text)
        self.confirm_text = text
        self.confirm_detail = detail
        self.confirm_style = style
        self.confirm_until = time.time() + seconds

    # ── 视图 ────────────────────────────────────────────────────────────────
    def build_view(self, now: float | None = None) -> View:
        now = now or time.time()
        if self.voice_preview is not None:
            return self._voice_view()
        if self.recording:
            return View(page=PAGE_VOICE, title="录音中", bar="danger",
                        lines=[ViewLine("● 正在听", "danger"),
                               ViewLine("松开 BtnGO 结束", "dim")],
                        footer="对着板子说话", hint="")
        if self.transcribing:
            return View(page=PAGE_VOICE, title="识别中", bar="alert",
                        lines=[ViewLine("把语音转成文字…", "body"),
                               ViewLine("通常 1-3 秒", "dim")],
                        footer="稍等", hint="")
        if self.pending is not None:
            return self._prompt_view(now)
        if self.reply_text and now - self.reply_at < self.reply_ttl:
            return self._reply_view(now)
        if self.confirm_text and now < self.confirm_until:
            return self._confirm_view(now)
        return self._status_view(now)

    def _reply_view(self, now: float) -> View:
        """助手回复单独成页：成段、可翻页，比塞进状态页一行 60 字有用得多。"""
        pages = self.reply_pages or [[self.reply_text]]
        self.reply_page = max(0, min(self.reply_page, len(pages) - 1))
        page = pages[self.reply_page]
        if isinstance(page, str):        # 防御：传进来的是整页字符串而不是行列表
            page = [page]
        lines = [ViewLine(t, "body", wrap=False) for t in page if t]
        if not lines:
            lines = [ViewLine("（空回复）", "dim", wrap=False)]
        n_q = len(self.reply_q) or 1
        bits = [f"回复 {self.reply_qi + 1}/{n_q}"]
        if len(pages) > 1:
            bits.append(f"页{self.reply_page + 1}/{len(pages)}")
        hints = "fn翻" + (" space换" if n_q > 1 else "") + " enter收"
        return View(page=PAGE_REPLY, title="Hermes 回复", bar="normal", lines=lines,
                    footer="  ".join(bits) + "  " + hints, hint="fn", dense=True)

    def _confirm_view(self, now: float) -> View:
        """按键生效后的结果页：整屏只讲一件事，别让人猜。

        footer 里不放倒计时——每秒变一次就等于每秒重发整屏（会闪）。
        """
        bar = "danger" if self.confirm_style == "danger" else "ok"
        lines = [ViewLine(self.confirm_text, "ok" if bar == "ok" else "danger")]
        if self.confirm_detail:
            lines.append(ViewLine(self.confirm_detail, "dim"))
        return View(page=PAGE_CONFIRM, title="Hermes 遥控器", bar=bar, lines=lines,
                    footer="已生效 · 稍后自动返回", hint="")

    def _status_view(self, now: float) -> View:
        row = self.active
        if not self.connected_backend:
            return View(page=PAGE_OFFLINE, title="Hermes 遥控器", bar="danger",
                        lines=[ViewLine("桥连不上 Hermes 后端", "danger"),
                               ViewLine("在 Mac 上运行 hermes-remote doctor", "dim")],
                        footer="重连中…", hint="")
        if row is None:
            return View(page=PAGE_STATUS, title="Hermes 遥控器", bar="normal",
                        lines=[ViewLine("还没有会话", "dim"),
                               ViewLine("在 Mac 上开一个 Hermes 会话", "dim")],
                        footer="1Hz 心跳", hint="")

        elapsed = ""
        if row.running and row.turn_started_at:
            secs = max(0, int(now - row.turn_started_at))
            # 只显示到分钟：按秒显示会让“画面签名”每秒都变，桥就得每秒重发整屏 → 屏上一秒闪一次
            if secs >= 60:
                elapsed = f"{secs // 60}m"     # 底栏寸土寸金：不再写"已跑"
        status = STATUS_LABEL.get(row.status, row.status or "待命")
        # 紧凑状态页：工具+结果成对滚动。一页 2 对（4 行）+ 最多 1 行补充
        entries = self.recent
        per_page = 2                      # 每页 2 个工具事件（保持"工具/结果"配对不被切开）
        self.max_scroll = max(0, (len(entries) - 1) // per_page)
        self.scroll = min(self.scroll, self.max_scroll)
        end = len(entries) - self.scroll * per_page
        window = entries[max(0, end - per_page):end] if end > 0 else []

        lines: list[ViewLine] = []
        # status_note 是"当前正在做的事"（tool.start 设、tool.complete 清）；
        # 如果它就是这个还没结束的工具，就别再单独占一行——否则同一个工具显示两遍。
        if self.scroll == 0 and self.status_note and not (
                entries and self.status_note.startswith(entries[-1]["tool"])):
            lines.append(ViewLine(self.status_note, "label", wrap=False))
        for entry in window:
            head = f"{entry['tool']} · {entry['target']}".strip(" ·")
            lines.append(ViewLine(head, "label", wrap=False))
            lines.append(ViewLine(entry["summary"] or "运行中…", "sub", wrap=False))
        if not lines:
            lines.append(ViewLine("正在等模型输出" if row.running else "空闲，等你的指令",
                                  "body", wrap=False))
        if self.scroll == 0:
            if self.last_key_result and now - self.last_key_at < 120:
                lines.append(ViewLine(self.last_key_result, "dim", wrap=False))
            # 还有空行就补一句"它最后说了什么"
            if len(lines) < 5 and self.last_reply:
                lines.append(ViewLine("说: " + self.last_reply, "dim", wrap=False))
            # 有回复但当前收起了：提示怎么回去（只在还有空行时占位）
            if len(lines) < 5 and self.reply_text:
                lines.append(ViewLine("[enter] 看上一条回复", "dim", wrap=False))
        lines = lines[:5]

        footer_bits = []
        if self.scroll:
            footer_bits.append(f"↑{self.scroll}页")
        if len(self.sessions) > 1:
            footer_bits.append(f"{self.active_index + 1}/{len(self.sessions)}")
        if self.queue_len:
            footer_bits.append(f"待批{self.queue_len}")
        footer_bits.append(f"{status}{elapsed}")
        if not self.scroll:               # 翻看历史时优先显示页码，电量/信号让位
            if self.device_battery is not None:
                footer_bits.append(f"电{self.device_battery}%")
            if self.device_rssi is not None:
                # 单位省掉（左边"电86%"已经给了语境）；弱信号才加感叹号
                weak = self.device_rssi < -75
                footer_bits.append(f"{self.device_rssi}{'!' if weak else ''}")
        footer = "  ".join(footer_bits)
        return View(page=PAGE_STATUS, title=row.short_title or "Hermes 遥控器",
                    bar="normal" if not row.running else "ok",
                    lines=lines, footer=footer, hint="[1-3]切会话 [侧键]说话", dense=True)

    def _prompt_view(self, now: float) -> View:
        req = self.pending
        assert req is not None
        title = self.active.short_title if self.active else "Hermes"
        if req.method == "approval":
            return self._approval_view(req, title)
        q = req.question
        text = str(q.get("question") or "它有一个问题要你回答")
        choices = req.choices
        if self.input_text is not None:
            return View(page=PAGE_INPUT, title="打字作答", bar="alert", dense=True,
                        lines=[ViewLine(text, "body"),
                               ViewLine(">" + self.input_text + "_", "ok", wrap=True)],
                        footer="[enter]发送 [del]删字 [esc]放弃", hint="enter")
        n = len(req.questions)
        head = f"问题 {req.q_index + 1}/{n}" if n > 1 else "等你决定"
        if req.age > 300:                 # 5 分钟没处理：后端多半早已不认这张卡
            head = "可能已过期"
        if self.opt_pages:
            total_opt = len(self.opt_pages)
            self.opt_page = max(0, min(self.opt_page, total_opt - 1))
            numbered = "\n".join(self.opt_pages[self.opt_page])
        else:
            total_opt = 1
            numbered = "  ".join(f"[{i + 1}]{c}" for i, c in enumerate(choices[:4]))
        # 问题正文可能很长：分页显示，最多 2 行一页（选项行永远保留）
        if self.q_pages:
            total = len(self.q_pages)
            self.q_page = max(0, min(self.q_page, total - 1))
            lines = [ViewLine(head, "label", wrap=False)]
            lines += [ViewLine(t, "body", wrap=False) for t in self.q_pages[self.q_page][:2]]
        else:
            total = 1
            lines = [ViewLine(head, "label", wrap=False), ViewLine(text, "body")]
        if req.confirm_armed and req.confirm_choice:
            lines.append(ViewLine(f"再按一次确认：{req.confirm_choice}", "danger"))
            footer = "确认中…  [esc]取消"
        else:
            for one in (numbered or "[侧键]说话作答").split("\n"):
                lines.append(ViewLine(one, "opt", wrap=True))
            if total > 1 or total_opt > 1:
                bits = []
                if total > 1:
                    bits.append(f"题{self.q_page + 1}/{total}")
                if total_opt > 1:
                    bits.append(f"选{self.opt_page + 1}/{total_opt}")
                footer = f"1-4 选 [fn]翻页 " + " ".join(bits)
                if self.queue_len > 1:
                    footer += " [space]换"
            elif not choices:
                footer = "按住侧键说，或直接打字"
            else:
                footer = "按 1-4 选，或按住侧键说"
        hint = "1234" if choices else ""
        return View(page=PAGE_PROMPT, title=title, bar="alert", lines=lines,
                    footer=footer, hint=hint, dense=True)

    def _approval_view(self, req: PendingRequest, title: str) -> View:
        from .safety import approval_key_map
        facts = req.approval  # 与按键处理共用同一份判定，渲染不再自行解释 params
        tool = facts.tool or "工具"

        if not facts.valid:
            lines = [ViewLine(f"不能在设备上批 · {tool}", "danger"),
                     ViewLine(facts.problem, "danger"),
                     ViewLine(f"回 Mac 上处理；这里只能拒绝", "dim")]
            if facts.context:
                lines[2] = ViewLine(facts.context[:34], "dim")
            return View(page=PAGE_PROMPT, title=title, bar="danger", lines=lines,
                        footer="[2]拒绝", hint="2")

        page = max(0, min(req.page, facts.page_count - 1))
        head = "危险操作" if facts.dangerous else "等你放行"
        if req.age > 300 and not req.confirm_armed:
            head = "可能已过期"
        lines = [ViewLine(f"{head} · {tool}", "danger" if facts.dangerous else "label")]
        for ln in (facts.pages[page] if facts.pages else ()):
            lines.append(ViewLine(ln, "body"))

        remaining = int(CONFIRM_WINDOW_S - (time.time() - req.confirm_at))
        armed = (req.confirm_armed and req.confirm_digest == facts.digest and remaining > 0)
        keymap = approval_key_map(req.choices)   # 固定键位，不随后端选项顺序走
        # 底栏只能放 ~34 个半角宽：标签必须短，否则 [2]拒绝 会被截掉（拒绝必须永远看得见）
        short = {"once": "同意", "deny": "拒绝", "session": "会话", "always": "永久"}
        numbered = " ".join(f"[{k}]{short.get(c, c)}" for k, c in keymap.items())
        pages = facts.page_count

        if armed:
            lines.append(ViewLine(f"再按一次确认，{remaining}s 内有效", "danger"))
            footer = "[esc]取消"
        elif pages > 1 and len(req.seen_pages) < pages:
            lines.append(ViewLine(f"第 {page + 1}/{pages} 页 · 翻完才能批", "danger"))
            footer = "[fn]翻页  [2]拒绝"
        elif pages > 1:
            footer = f"{page + 1}/{pages}  {numbered}"
        else:
            footer = numbered
        return View(page=PAGE_PROMPT, title=title, bar="danger" if facts.dangerous else "alert",
                    lines=lines, footer=footer, hint="1-4")

    def _voice_view(self) -> View:
        text = self.voice_preview or ""
        pages = self.voice_pages or [[text or "…"]]
        self.voice_page = max(0, min(self.voice_page, len(pages) - 1))
        lines = [ViewLine("转写结果", "label", wrap=False)]
        lines += [ViewLine(t, "body", wrap=False) for t in pages[self.voice_page][:4] if t]
        if not lines[1:]:
            lines.append(ViewLine("…", "dim", wrap=False))
        # 底栏按宽度精简：页数 + 键位提示
        bits = [f"{self.voice_page + 1}/{len(pages)}"] if len(pages) > 1 else []
        bits.append("[fn]翻" if len(pages) > 1 else "enter发")
        bits.append("[enter]发出")
        bits.append("[mic]重录")
        footer = " ".join(bits) if len(pages) > 1 else "[enter]发出  [mic]重录  [esc]放弃"
        return View(page=PAGE_VOICE, title="说的话（确认才发）", bar="ok",
                    lines=lines, footer=footer, hint="enter", dense=True)
