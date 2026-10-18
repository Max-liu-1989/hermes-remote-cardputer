"""审批的安全边界：什么算危险、屏上必须写出什么、答案长什么样。

这里的规则不是“多加功能”，是为了避免半年后出现一件解释不清的事：
遥控器上的一个 `1` 可能放行一条 `rm -rf`。所以：
  * 屏上必须写清“这条命令属于哪个会话、是哪个工具、命令原文是什么”；
  * 命令截断时保留开头（动词）和结尾（目标），中段才省略；
  * 危险操作按两次才生效；
  * 每次远程作答都写一行审计日志。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 命中即视为危险操作，需要二次确认
DANGEROUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("递归删除", re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+", re.I)),
    ("强推", re.compile(r"\bgit\s+push\b.*(--force|-f)\b", re.I)),
    ("重写历史", re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f)", re.I)),
    ("提权", re.compile(r"\bsudo\b|\bsu\s+-", re.I)),
    ("磁盘操作", re.compile(r"\b(mkfs|fdisk|parted|dd\s+if=)", re.I)),
    ("改权限", re.compile(r"\bchmod\s+(-R\s+)?777\b|\bchown\s+-R\b", re.I)),
    ("系统电源", re.compile(r"\b(shutdown|reboot|halt|launchctl\s+unload|systemctl\s+(stop|disable))\b", re.I)),
    ("杀进程", re.compile(r"\bkill\s+-9\b|\bpkill\s+-9\b", re.I)),
    ("管道执行", re.compile(r"(curl|wget)[^|]*\|\s*(ba)?sh\b", re.I)),
    ("数据库写入", re.compile(r"\b(drop\s+table|truncate\s+table|delete\s+from)\b", re.I)),
    ("发布", re.compile(r"\b(npm\s+publish|twine\s+upload|docker\s+push)\b", re.I)),
    ("生产部署", re.compile(r"\b(kubectl\s+(apply|delete)|terraform\s+apply|ansible-playbook)\b", re.I)),
    ("覆盖配置", re.compile(r">\s*/(etc|boot|usr)/", re.I)),
]

#: 这些工具名本身就意味着“能对自己下手”：命中即一律做结构判定
DANGEROUS_TOOLS = frozenset({
    "terminal", "terminal_code", "shell", "bash", "sh", "zsh", "exec",
    "process_manage", "computer", "computer_use", "browser_exec", "code_execution",
})

#: 参数里的命令动词：只要第一个词落在里面，就算没有命中任何正则，也要二次确认
WRITE_VERBS = frozenset("""
rm rmdir mv cp dd tee truncate chmod chown chgrp ln install
sed awk kill pkill killall shutdown reboot halt poweroff
mkfs fdisk parted mount umount diskutil hdiutil
launchctl systemctl service defaults pmset crontab at osascript spctl codesign xattr
brew npm yarn pnpm pip pip3 uv poetry cargo go docker kubectl helm terraform
ansible ansible-playbook rsync scp sftp ssh curl wget
""".split())

#: git 里只读的子命令（其余子命令按写操作处理）
GIT_READONLY = frozenset({"status", "diff", "log", "show", "rev-parse", "branch",
                          "remote", "blame", "grep", "describe", "fetch", "tag", "stash"})

#: shell 组合/重定向：能藏第二条命令，或者把结果写到任何地方
_SHELL_META = re.compile(r"[|;&<>`]|\$\(")
#: 内联脚本：python -c / node -e 这种，命令原文根本看不出它要干什么
_INLINE_SCRIPT = re.compile(r"\b(python[0-9.]*|perl|ruby|node|php|lua)\b[^|;&]*\s-(c|e)\b", re.I)
#: find 的写操作
_FIND_WRITE = re.compile(r"\bfind\b[^|;&]*\s-(delete|exec|execdir|ok)\b", re.I)


def _first_token(command: str) -> str:
    """命令的第一个词（跳过环境变量赋值与 sudo）。"""
    for raw in (command or "").strip().split():
        if "=" in raw and not raw.startswith("-"):
            continue  # FOO=1 rm ... 里的赋值
        if raw in ("sudo", "doas", "env"):
            continue
        return raw.rsplit("/", 1)[-1].lower()
    return ""


def _terminal_structural_hits(command: str) -> list[str]:
    """终端类工具的“结构判定”：不看它像不像危险，看它能干什么。"""
    hits: list[str] = []
    text = command or ""
    if _SHELL_META.search(text):
        hits.append("shell 组合/重定向")
    if _INLINE_SCRIPT.search(text):
        hits.append("内联脚本")
    if _FIND_WRITE.search(text):
        hits.append("find 写操作")
    token = _first_token(text)
    if token == "python" or token.startswith("python3"):
        if re.search(r"\s-c\b", text):
            hits.append("内联脚本")
    elif token == "git":
        parts = [p for p in text.split() if not p.startswith("-")]
        sub = parts[1].lower() if len(parts) > 1 else ""
        if sub not in GIT_READONLY:
            hits.append("git 写操作")
    elif token in WRITE_VERBS:
        hits.append(f"写入类命令（{token}）")
    return hits


def matched_dangers(command: str, tool: str = "") -> list[str]:
    """返回命中的危险标签；空列表 = 单次按键即可放行。

    判定分两层：
      1. 文本层：命令原文命中 ``DANGEROUS_PATTERNS``；
      2. 结构层：``tool`` 属于 ``DANGEROUS_TOOLS`` 时，再看它有没有 shell 组合、
         内联脚本、写入类动词——因为一条 ``python3 -c`` 的破坏力在正则里是看不出来的。
    没有命令原文时按危险处理（屏上本来就让用户按 2 拒绝，逻辑必须和它一致）。
    """
    text = (command or "").strip()
    hits = [label for label, pat in DANGEROUS_PATTERNS if pat.search(text)]
    if not text:
        hits.append("没有命令原文")
        return hits
    if (tool or "").strip().lower() in DANGEROUS_TOOLS:
        hits.extend(_terminal_structural_hits(text))
    # 去重但保持顺序
    seen: set[str] = set()
    out: list[str] = []
    for hit in hits:
        if hit not in seen:
            seen.add(hit)
            out.append(hit)
    return out


def approval_is_dangerous(command: str, tool: str = "") -> bool:
    return bool(matched_dangers(command, tool))


def _middle_ellipsis(text: str, keep_head: int, keep_tail: int) -> str:
    if len(text) <= keep_head + keep_tail + 1:
        return text
    return f"{text[:keep_head]}…{text[-keep_tail:]}"


def approval_command_lines(command: str, *, per_line: int = 34, max_lines: int = 2) -> list[str]:
    """把命令拆成屏上能显示的 1-2 行；超长时保留动词和目标，省略中段。

    34 个 ASCII 字符 ≈ 228px（12px 字体），正好是屏宽留给正文的宽度；
    交给渲染器再截断就会把“目标”那一半切掉，所以这里先按屏宽切好。
    """
    text = " ".join((command or "").split())
    if not text:
        return ["（没有命令原文，按 2 拒绝更安全）"]
    if len(text) <= per_line:
        return [text]
    if len(text) <= per_line * max_lines:
        return [text[i:i + per_line] for i in range(0, len(text), per_line)][:max_lines]
    # 太长：第一行留开头（动词），最后一行留结尾（目标），中段省略
    return [text[:per_line - 1] + "…", "…" + text[-(per_line - 1):]]


def approval_choice_label(choice: str) -> str:
    return {"once": "同意一次", "session": "本会话同意", "always": "永久同意",
            "deny": "拒绝"}.get(choice, choice)


# 审批页的键位是**固定的**，不跟后端返回的顺序走：
# 后端给的是 once/session/always/deny，天然后端顺序编号时 2 会落在“本会话都同意”上，
# 用户以为按 2 是拒绝，实际按成了整会话放行（真机上已经发生过一次）。
# 规则：1=同意一次，2=拒绝（永远存在），3/4 才是更宽的授权。
APPROVAL_KEY_ORDER = ("once", "deny", "session", "always")


def approval_key_map(available) -> dict[str, str]:
    """把 ``available``（后端给的选项）映射成固定键位 ``{"1": "once", "2": "deny", ...}``。

    ``deny`` 无条件占 2 号键：拒绝必须随时可用，哪怕后端没给这一项。
    """
    have = {str(c) for c in (available or ())}
    have.add("deny")
    out: dict[str, str] = {}
    for idx, choice in enumerate(APPROVAL_KEY_ORDER, start=1):
        if choice in have and idx <= 4:
            out[str(idx)] = choice
    return out


# ── 统一审批模型 ──────────────────────────────────────────────────────────────
#
# 屏上显示什么、按键能不能批、要不要二次确认，必须是**同一份**判定结果。
# 以前是渲染和按键各自从原始 params 里重新解释字段（渲染还会拿 description
# 顶替 command），于是出现“屏上写着危险、一次按键却放行”。
# 现在两者都只读 ApprovalFacts，不许再碰原始字段。

LINE_CHARS = 22       # 17px 字体下屏宽真正放得下的 ASCII 字符数（26 会把单词切成两半）
LINES_PER_PAGE = 2    # 审批页正文两行
MAX_PAGES = 10        # 超过 20 行（约 440 字符）就不让在设备上批


@dataclass(frozen=True)
class ApprovalFacts:
    """一次审批请求的“事实”：能不能看到、看到什么、算不算危险、怎么翻页。"""

    tool: str
    command: str
    valid: bool
    problem: str
    context: str          # description 之类的补充文字，只做说明，不代替命令
    dangerous: bool
    reasons: tuple[str, ...]
    pages: tuple[tuple[str, ...], ...]

    @property
    def digest(self) -> str:
        import hashlib

        return hashlib.sha1(f"{self.tool}\x00{self.command}".encode()).hexdigest()[:16]

    @property
    def page_count(self) -> int:
        return max(1, len(self.pages))


def wrap_command(command: str, *, per_line: int = LINE_CHARS,
                 per_page: int = LINES_PER_PAGE, max_pages: int = MAX_PAGES) -> tuple[tuple[str, ...], ...]:
    """把命令按屏宽切成页；优先在空格处断。返回至少一页。"""
    words = command.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        while len(word) > per_line:  # 单个超长 token：硬切
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:per_line])
            word = word[per_line:]
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= per_line:
            cur += " " + word
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if not lines:
        lines = [""]
    pages = [tuple(lines[i:i + per_page]) for i in range(0, len(lines), per_page)]
    return tuple(pages[:max_pages])


def approval_facts(params: dict) -> ApprovalFacts:
    """校验并归一化一次 approval 请求。**渲染和按键都只读它的结果。**"""
    params = params if isinstance(params, dict) else {}
    tool = str(params.get("tool_name") or "")
    context = " ".join(str(params.get("description") or "").split())[:80]
    raw = params.get("command")

    if raw is None:
        return ApprovalFacts(tool=tool, command="", valid=False,
                             problem="没有命令原文", context=context,
                             dangerous=True, reasons=("没有命令原文",), pages=((),))
    if not isinstance(raw, str):
        return ApprovalFacts(tool=tool, command="", valid=False,
                             problem="命令类型不对", context=context,
                             dangerous=True, reasons=("命令类型不对",), pages=((),))
    text = " ".join(raw.split())
    if not text:
        return ApprovalFacts(tool=tool, command="", valid=False,
                             problem="命令为空", context=context,
                             dangerous=True, reasons=("命令为空",), pages=((),))
    pages = wrap_command(text)
    if len(pages) >= MAX_PAGES and len(text) > LINE_CHARS * LINES_PER_PAGE * MAX_PAGES:
        return ApprovalFacts(tool=tool, command=text, valid=False,
                             problem="命令过长，屏上无法完整查阅", context=context,
                             dangerous=True, reasons=("命令过长",), pages=pages)
    reasons = tuple(matched_dangers(text, tool))
    return ApprovalFacts(tool=tool, command=text, valid=True, problem="", context=context,
                         dangerous=bool(reasons), reasons=reasons, pages=pages)


def approval_requires_double_press(facts: ApprovalFacts) -> bool:
    """能不能在设备上批、要不要两次。无效请求连一次都不行，由调用方先拦。"""
    return facts.dangerous


def approval_result(req_params: dict, choice: str) -> dict:
    """approval 的 result 走 resolve_response：{choice}。"""
    return {"choice": choice}


def clarify_result(answers: dict[str, str | None]) -> dict:
    """clarify 的 result：{answers: {qid: 文本或 null}}。null = 跳过该题。"""
    return {"answers": dict(answers)}


def answer_text_for_choice(choices: list[str], index: int) -> str | None:
    if 0 <= index < len(choices):
        return choices[index]
    return None
