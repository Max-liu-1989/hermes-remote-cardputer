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

import logging
import os
import re
import subprocess
from dataclasses import dataclass

log = logging.getLogger("hermes_remote.desktop_discovery")

TOKEN_ENV = "HERMES_DASHBOARD_SESSION_TOKEN"
DESKTOP_ENV = "HERMES_DESKTOP"

_PORT_RE = re.compile(r"^n(?:127\.0\.0\.1|\[::1\]|localhost):(\d+)$")


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


def find_desktop_backend() -> DesktopBackend | None:
    """返回桌面 App 的后端（pid/port/token）；找不到就返回 None。"""
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
