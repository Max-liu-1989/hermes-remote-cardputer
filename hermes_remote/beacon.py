"""UDP 广播发现：桥每 5 秒在局域网喊一次自己的端口。

板子只记桥的 IP:port，不记域名——旁路由的 Clash fake-ip 劫持不到广播包。
除了 255.255.255.255，还会对每张网卡发子网定向广播：macOS 多网卡
（WiFi + 有线 + Clash TUN）下 255.255.255.255 经常发不对口。
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket

log = logging.getLogger("hermes_remote.beacon")

BEACON_PORT = 47824
BEACON_INTERVAL_S = 5.0


def _subnet_broadcasts() -> list[str]:
    addrs: list[str] = []
    try:
        import subprocess
        if hasattr(socket, "AF_INET"):
            # macOS/Linux 通用的做法：用 route/getifaddrs 太重，改用 UDP connect 探测本机地址
            for target in ("10.255.255.255", "192.168.255.255", "172.31.255.255"):
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.connect((target, 1))
                    local = s.getsockname()[0]
                    s.close()
                    parts = local.split(".")
                    if len(parts) == 4 and parts[0] != "127":
                        addrs.append(f"{parts[0]}.{parts[1]}.{parts[2]}.255")
                except OSError:
                    continue
    except Exception:
        pass
    return addrs


class Beacon:
    def __init__(self, port: int, host_token: str = "", name: str | None = None,
                 beacon_port: int = BEACON_PORT) -> None:
        self.port = port
        self.name = name or socket.gethostname()
        self.beacon_port = beacon_port
        # token 不进广播包：发现是公开的，连接才鉴权
        self.payload = json.dumps({"t": "hermes-remote", "v": 1, "port": port,
                                   "name": self.name}, ensure_ascii=False).encode()
        self._task: asyncio.Task | None = None
        self._sock: socket.socket | None = None

    async def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        self._sock = sock
        self._task = asyncio.create_task(self._loop(), name="beacon")
        log.info("beacon on udp/%d → port %d", self.beacon_port, self.port)

    async def _loop(self) -> None:
        targets = ["255.255.255.255"] + _subnet_broadcasts()
        while True:
            for target in targets:
                try:
                    self._sock.sendto(self.payload, (target, self.beacon_port))
                except OSError:
                    continue
            await asyncio.sleep(BEACON_INTERVAL_S)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None


class BeaconListener:
    """板子侧行为的 Python 版：测试和一个“模拟板子”都用它。"""

    def __init__(self, port: int = BEACON_PORT) -> None:
        self.port = port

    def listen_once(self, timeout: float = 6.0) -> dict | None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.port))
        sock.settimeout(timeout)
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            return None
        finally:
            sock.close()
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return None
        payload["host"] = addr[0]
        return payload
