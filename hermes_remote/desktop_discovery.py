"""在本机找出“桌面 App 起的那个 Hermes 后端”，并拿到它的 port + token。

为什么需要这个：桌面 App 用 Electron 起后端时，token 只注入子进程的环境变量
（``apps/desktop/electron/main.ts`` 里 ``HERMES_DASHBOARD_SESSION_TOKEN: token``），
ready file 只写 ``{"port": N}``。桥是另一个进程，平时没有任何正规途径读到它。

这里的做法：在**同一个 uid** 的进程里，找 ``HERMES_DESKTOP=1`` 且带
``HERMES_DASHBOARD_SESSION_TOKEN`` 的那个 python 进程（``ps eww`` 读自己进程的
环境不需要提权），再用 ``lsof`` 读它监听的 127.0.0.1 端口。

边界：
  * 只用 ``ps`` / ``lsof`` 两个只读命令，不写、不试、不猜；
  * token 不打印、不进日志，直接写进 0600 的配置文件；
  * 桌面 App 重启后端口会变 → ``run --rediscover`` 会在断线时重新发现一次。
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("hermes_remote.desktop_discovery")

TOKEN_ENV = "HERMES_DASHBOARD_SESSION_TOKEN"
DESKTOP_ENV = "HERMES_DESKTOP"

#: 桥自己保存后端 token 的位置（0600）。0.22.x 起桌面端不再把 token 注入可读环境，
#: 改为由 launchd 的 gateway 从 ``HERMES_DASHBOARD_SESSION_TOKEN`` 读固定值，桥读同一份。
TOKEN_PATH = Path.home() / ".hermes-remote" / "backend.token"

_PORT_RE = re.compile(r"^n(?:127\.0\.0\.1|\[::1\]|localhost):(\d+)$")
_ORIGIN_RE = re.compile(r"^https?://(?:127\.0\.0\.1|\[::1\]|localhost):(\d+)$")


@dataclass(frozen=True)
class DesktopBackend:
    pid: int
    port: int
    token: str

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/ws"


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s failed: %s", cmd[0], exc)
        return ""
    return res.stdout


def _candidate_pids() -> list[int]:
    """当前 uid 的 python 进程（桌面后端是 python -I -c，不是 Electron 本身）。"""
    out = _run(["ps", "-Ao", "pid=,uid=,comm="])
    me = os.getuid()
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, uid_s, comm = parts
        if not pid_s.isdigit() or not uid_s.isdigit() or int(uid_s) != me:
            continue
        if "python" not in comm.lower():
            continue
        pids.append(int(pid_s))
    return pids


def _env_of(pid: int) -> dict[str, str]:
    """读一个进程的环境变量（自己的进程，不需要 root）。"""
    out = _run(["ps", "eww", "-p", str(pid)])
    if not out.strip():
        return {}
    body = out.split(None, 4)
    env_blob = body[4] if len(body) > 4 else ""
    env: dict[str, str] = {}
    for item in env_blob.split():
        key, sep, value = item.partition("=")
        if sep and key and key not in env:
            env[key] = value
    return env


def _listen_port(pid: int) -> int | None:
    out = _run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-F", "n"])
    for line in out.splitlines():
        m = _PORT_RE.match(line.strip())
        if m:
            return int(m.group(1))
    return None


def _control_query(home: Path, payload: dict, timeout: float = 5.0) -> dict | None:
    """向 gateway 的 control socket 发一条 JSON 请求，返回解析后的响应。

    ``~/.hermes/gateway.sock`` 是网关自己的本地协调面（``gateway/control_socket.py``），
    只接受同 uid 的 peer —— 桥和 gateway 是同一个用户，所以这是官方留的正规入口。
    """
    path = home / "gateway.sock"
    if not path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
    except (OSError, ValueError) as exc:
        log.debug("control socket %s failed: %s", path, exc)
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except ValueError as exc:
        log.debug("control socket returned non-JSON: %s", exc)
        return None


def _read_token_file() -> str:
    try:
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def find_gateway_backend() -> DesktopBackend | None:
    """通过 control socket 找网关：``identify`` 拿 ``api_origin``，token 读 0600 文件。

    0.22.x 起桌面端把后端交给 launchd 托管（``gateway run``），端口随机、token 不再
    注入任何可读环境 —— 老办法（``ps eww`` 找 ``HERMES_DESKTOP=1``）在新版必然失败。
    ``identify`` 直接回 ``api_origin``，这是端口唯一可靠的来源。
    """
    home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    resp = _control_query(home, {"protocol": 1, "verb": "identify", "id": 1})
    if not resp or resp.get("ok") is not True:
        return None
    result = resp.get("result")
    if not isinstance(result, dict):
        return None
    match = _ORIGIN_RE.match(str(result.get("api_origin") or ""))
    if not match:
        log.debug("identify 没给出可用的 api_origin: %r", result.get("api_origin"))
        return None
    token = _read_token_file()
    if not token:
        log.debug("缺失后端 token 文件 %s", TOKEN_PATH)
        return None
    return DesktopBackend(pid=int(result.get("pid") or 0), port=int(match.group(1)), token=token)


def find_desktop_backend() -> DesktopBackend | None:
    """返回桌面 App 的后端（pid/port/token）；找不到就返回 None。"""
    # 新版（gateway 托管）优先：control socket 是官方入口，端口随机也能拿到。
    found = find_gateway_backend()
    if found is not None:
        return found
    # 旧版（Electron 起独立 serve，token 注入子进程环境）保留作回退。
    for pid in _candidate_pids():
        env = _env_of(pid)
        token = env.get(TOKEN_ENV, "")
        if not token:
            continue
        if env.get(DESKTOP_ENV) != "1":
            continue  # 不是桌面 App 起的后端（比如自己开的 dashboard）
        port = _listen_port(pid)
        if port is None:
            continue
        return DesktopBackend(pid=pid, port=port, token=token)
    return None


def describe() -> str:
    found = find_desktop_backend()
    if found is None:
        return "没找到桌面 App 的后端（桌面 App 没开？或者它没有监听 127.0.0.1）"
    return f"桌面后端 pid={found.pid} 127.0.0.1:{found.port}（token 已读到，不显示）"
