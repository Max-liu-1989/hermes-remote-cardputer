"""远程作答的审计日志：一行一次，JSONL，权限 0600。

遥控器上按下的每一个“同意”，都必须在 Mac 上留下一行能对上的记录。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

DEFAULT_AUDIT_PATH = Path("~/.hermes-remote/audit.jsonl")


class AuditLog:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        # 注意：默认路径这里也必须 expanduser()。交付版漏了这一步，
        # 不带 --audit 运行时会在当前目录下建一个字面量 "./~/.hermes-remote/audit.jsonl"。
        self.path = (Path(path).expanduser() if path else DEFAULT_AUDIT_PATH.expanduser())
        self._ready = False

    def _ensure(self) -> None:
        if self._ready:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        self._ready = True

    def write(self, event: str, **fields: object) -> None:
        record = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                  "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False)
        try:
            self._ensure()
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            # 审计写不进去不阻塞放行，但必须留下痕迹到 stderr
            import sys
            print(f"[audit-failed] {line}", file=sys.stderr, flush=True)

    def tail(self, n: int = 20) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
