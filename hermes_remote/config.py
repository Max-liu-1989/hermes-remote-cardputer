"""配置与后端发现。

优先级（高 → 低）：
  1. 命令行 --url / --token
  2. ~/.hermes-remote/config.json（0600）
  3. 环境变量 HERMES_REMOTE_BACKEND_URL / HERMES_REMOTE_BACKEND_TOKEN
  4. 环境变量 HERMES_DASHBOARD_SESSION_TOKEN（桌面 App 铸给后端的同一个值）
  5. 桌面后端的 ready file（HERMES_DESKTOP_READY_FILE，JSON {"port": N}）
  6. 默认 127.0.0.1:9119（`hermes dashboard` 的默认端口）

端口和 token 都不写死在固件里：token 只存在板子的 NVS 与这台 Mac 的 0600 文件里。
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field, asdict
from pathlib import Path

HOME = Path("~/.hermes-remote").expanduser()
CONFIG_PATH = HOME / "config.json"
AUDIT_PATH = HOME / "audit.jsonl"
DEVICE_TOKEN_PATH = HOME / "device.token"


@dataclass
class Config:
    backend_url: str = ""
    backend_token: str = ""
    lan_port: int = 47823
    device_token: str = ""
    session_id: str | None = None
    stt_model: str = "large-v3-turbo"
    beacon: bool = True

    def to_json(self) -> dict:
        data = asdict(self)
        data["backend_token"] = "***" if self.backend_token else ""
        return data


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def discover_backend_port() -> int | None:
    ready = os.environ.get("HERMES_DESKTOP_READY_FILE")
    if ready:
        data = _read_json(Path(ready).expanduser())
        port = data.get("port")
        if isinstance(port, int) and port > 0:
            return port
    env_port = os.environ.get("HERMES_REMOTE_BACKEND_PORT")
    if env_port and env_port.isdigit():
        return int(env_port)
    return None


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    data = _read_json(path)
    cfg = Config()
    for key, value in data.items():
        if hasattr(cfg, key) and value is not None:
            setattr(cfg, key, value)
    if not cfg.backend_token:
        cfg.backend_token = (os.environ.get("HERMES_REMOTE_BACKEND_TOKEN")
                             or os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN") or "")
    if not cfg.device_token:
        cfg.device_token = _load_or_create_device_token()
    return cfg


def _load_or_create_device_token() -> str:
    env = os.environ.get("HERMES_REMOTE_DEVICE_TOKEN")
    if env:
        return env
    try:
        if DEVICE_TOKEN_PATH.exists():
            token = DEVICE_TOKEN_PATH.read_text(encoding="utf-8").strip()
            if len(token) >= 32:
                return token
        token = secrets.token_hex(16)
        DEVICE_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(DEVICE_TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        return token
    except OSError:
        return secrets.token_hex(16)


def save_config(cfg: Config, path: Path | None = None) -> Path:
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(cfg)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def resolve_backend_url(cfg: Config, *, cli_url: str = "", cli_token: str = "") -> str:
    """把 url 拼出来；命令行 > 配置 > 环境 > 发现 > 默认端口。"""
    if cli_url:
        from urllib.parse import urlencode, urlsplit, urlunsplit

        if cli_token and "token=" not in cli_url:
            parts = urlsplit(cli_url)
            query = parts.query + ("&" if parts.query else "") + urlencode({"token": cli_token})
            return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
        return cli_url
    from .gateway import DEFAULT_PORT, build_url

    token = cli_token or cfg.backend_token
    if cfg.backend_url:
        if token and "token=" not in cfg.backend_url:
            sep = "&" if "?" in cfg.backend_url else "?"
            return f"{cfg.backend_url}{sep}token={token}"
        return cfg.backend_url
    port = discover_backend_port() or DEFAULT_PORT
    return build_url(port=port, token=token)
